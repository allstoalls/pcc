"""Python frontend compilation pipeline.

Orchestrates the full pipeline for a single ``.py`` file:

    source(.py)
      -> pcc.py_frontend.parser.parse()        -> Module AST
      -> pcc.py_frontend.type_infer.infer_module() -> typed Module
      -> pcc.py_frontend.codegen.layer1.L1CodeGen().generate() -> LLVM IR text
      -> write .ll to a temp file
      -> clang .ll + pcc/py_runtime/libpy_runtime.a -> native exe

This is the Phase 1 MVP dispatcher. See
``docs/plans/python-frontend-interfaces.md`` for the frozen v0.1
interface contract and ``docs/plans/python-frontend-plan.md`` for the
Phase 1 scope.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from .export_meta import encode_type


# Resolve pcc/py_runtime/ at import time. In CPython source mode this
# file lives under ``.../pcc/py_frontend/``; in compiled bootstrap mode
# ``__file__`` can already point one level higher. Probe a small set of
# stable layouts and keep the first directory that exists.
_PIPELINE_DIR = str(os.path.dirname(os.path.abspath(__file__)))
_PCC_DIR = str(os.path.dirname(_PIPELINE_DIR))

def _runtime_dir_has_runtime_files(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    include_h = os.path.isfile(os.path.join(path, "include", "py_runtime.h"))
    makefile = os.path.isfile(os.path.join(path, "Makefile"))
    maybe_lib = os.path.isfile(os.path.join(path, "libpy_runtime.a"))
    return include_h or makefile or maybe_lib


_PY_RUNTIME_DIR_CANDIDATE_1 = str(os.path.join(_PCC_DIR, "pcc", "runtime"))
_PY_RUNTIME_DIR_CANDIDATE_2 = str(os.path.join(_PCC_DIR, "py_runtime"))
_PY_RUNTIME_DIR_CANDIDATE_3 = str(os.path.join(_PIPELINE_DIR, "py_runtime"))
_PY_RUNTIME_DIR_CANDIDATE_4 = str(os.path.join(
    _PIPELINE_DIR, "pcc", "runtime",
))
_PY_RUNTIME_DIR_CANDIDATE_5 = str(os.path.join(
    os.getcwd(), "pcc", "runtime",
))
_PY_RUNTIME_DIR = str(
    _PY_RUNTIME_DIR_CANDIDATE_1
    if _runtime_dir_has_runtime_files(_PY_RUNTIME_DIR_CANDIDATE_1)
    else _PY_RUNTIME_DIR_CANDIDATE_2
    if _runtime_dir_has_runtime_files(_PY_RUNTIME_DIR_CANDIDATE_2)
    else _PY_RUNTIME_DIR_CANDIDATE_3
    if _runtime_dir_has_runtime_files(_PY_RUNTIME_DIR_CANDIDATE_3)
    else _PY_RUNTIME_DIR_CANDIDATE_4
    if _runtime_dir_has_runtime_files(_PY_RUNTIME_DIR_CANDIDATE_4)
    else _PY_RUNTIME_DIR_CANDIDATE_5
)

if os.environ.get("PCC_DEBUG_RUNTIME", "").strip():
    try:
        with open("/tmp/pcc_runtime_debug_probe.txt", "a", encoding="utf-8") as _f:
            _f.write("[probe] _PIPELINE_DIR=" + _PIPELINE_DIR + "\n")
            _f.write("[probe] _PCC_DIR=" + _PCC_DIR + "\n")
            _f.write("[probe] candidates="
                     + ",".join([
                         _PY_RUNTIME_DIR_CANDIDATE_1,
                         _PY_RUNTIME_DIR_CANDIDATE_2,
                         _PY_RUNTIME_DIR_CANDIDATE_3,
                         _PY_RUNTIME_DIR_CANDIDATE_4,
                     ])
                     + "\n")
    except Exception:
        pass
_PY_RUNTIME_ARCHIVE = str(os.path.join(_PY_RUNTIME_DIR, "libpy_runtime.a"))
_PY_RUNTIME_ARCHIVE_LIBPYTHON = str(os.path.join(
    _PY_RUNTIME_DIR, "libpy_runtime_libpython.a",
))
_PY_RUNTIME_ARCHIVE_PCC = str(os.path.join(
    _PY_RUNTIME_DIR, "libpy_runtime_pcc.a",
))
_PY_RUNTIME_ARCHIVE_PCC_PY = str(os.path.join(
    _PY_RUNTIME_DIR, "libpy_runtime_pcc_py.a",
))
_PY_RUNTIME_ARCHIVE_PCC_PY_LIBPYTHON = str(os.path.join(
    _PY_RUNTIME_DIR, "libpy_runtime_pcc_py_libpython.a",
))
_PY_LIBPYTHON_MODE_ENV = "PCC_PYTHON_LIBPYTHON"
_IR_SCAFFOLD_MODE_ENV = "PCC_IR_SCAFFOLD"
_PYTHON_IR_PASSES_ENV = "PCC_PYTHON_IR_PASSES"
_PY_RUNTIME_CC_ENV = "PCC_RUNTIME_CC"
_PY_RUNTIME_HIGH_ENV = "PCC_RUNTIME_HIGH"
_SELF_BACKEND_JOBS_ENV = "PCC_SELF_BACKEND_JOBS"
_COMPILE_TIME_ONLY_IMPORT_FROMS = {
    "abc": frozenset({"ABC", "abstractmethod"}),
    "dataclasses": frozenset({"dataclass", "field", "replace"}),
}
_COMPILE_TIME_ONLY_IMPORT_MODULES = frozenset({
    "__future__", "typing", "click", "abc",
})
_ANNOTATION_ONLY_IMPORT_MODULES = frozenset({
    "llvmlite.binding",
    "llvmlite.ir",
})
_NATIVE_BUILTIN_IMPORTS = frozenset({
    "sys", "os", "platform", "subprocess", "asyncio", "tempfile", "shutil",
    "shlex", "sysconfig", "math", "re", "gc", "weakref",
})
_NATIVE_IMPORT_FROMS = {
    "sys": frozenset({"exit", "stdout", "stderr"}),
    "os": frozenset({"path"}),
    "math": frozenset({"floor", "sqrt"}),
    "re": frozenset({"match"}),
    "gc": frozenset({
        "collect", "disable", "enable", "isenabled", "is_tracked",
        "get_count", "get_threshold", "set_threshold",
    }),
    "weakref": frozenset({"ref"}),
    "asyncio": frozenset({"run", "sleep"}),
}
_SCAFFOLD_IMPORT_MODULES = frozenset({
    "pcc.extern", "pcc.llvm_capi", "pcc.llvm_capi.compat", "pcc.unsafe",
})
_PYTHON_IR_PASS_FAST_PRESET = (
    "mem2reg",
    "sroa",
    "early-cse",
    "instsimplify",
    "function-attrs",
    "adce",
    "dce",
)
_PYTHON_IR_PASS_PRESETS = {
    "quick": ("mem2reg", "sroa", "sccp", "dce"),
    "fast": _PYTHON_IR_PASS_FAST_PRESET,
    "default": _PYTHON_IR_PASS_FAST_PRESET,
    "all": ("all",),
    "full": ("all",),
}
_SELF_BACKEND_HOST_CODE = (
    "import sys\n"
    "from pcc.backend.self_backend_dispatch import emit_self_asm, "
    "self_backend_target_identity\n"
    "from pcc.backend.self_backend_parse import "
    "parse_self_backend_target_triple\n"
    "path = sys.argv[1]\n"
    "with open(path, 'r', encoding='utf-8') as f:\n"
    "    text = f.read()\n"
    "triple = parse_self_backend_target_triple(text)\n"
    "sys.stdout.write(self_backend_target_identity(triple) + '\\n')\n"
    "sys.stdout.write(emit_self_asm(text))\n"
)


class PyPipelineError(RuntimeError):
    """Raised when the Python pipeline fails in a user-visible way."""


def _normalize_native_backend_name(value: Optional[str]) -> str:
    if value is None:
        value = os.environ.get("PCC_BACKEND")
    candidate = str(value or "").strip().lower()
    if not candidate:
        return "llvm"
    if candidate == "llvmlite":
        return "llvm"
    if candidate == "llvm-capi":
        return "llvm_capi"
    return candidate


def _resolve_native_backend(backend: Optional[str]) -> str:
    kind = _normalize_native_backend_name(backend)
    if kind not in ("llvm", "self"):
        if kind == "llvm_capi":
            raise PyPipelineError(
                "Python native emission backend "
                f"{kind!r} is not supported; expected llvm or self"
            )
        raise PyPipelineError(
            "unknown backend "
            f"{kind!r}; expected one of: llvm, llvm_capi, self"
        )
    return kind


def _native_backend_kind(backend) -> str:
    kind = str(getattr(backend, "kind", backend) or "")
    if kind not in ("llvm", "self"):
        raise PyPipelineError(
            "Python native emission backend "
            f"{kind!r} is not supported; expected llvm or self"
        )
    return kind


def _resolve_libpython_mode(mode: Optional[str]) -> str:
    raw = mode
    if raw is None:
        raw = os.environ.get(_PY_LIBPYTHON_MODE_ENV, "")
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
        "invalid libpython mode "
        f"{raw!r}; expected auto, on, or off"
    )


def _resolve_ir_scaffold_mode(mode: Optional[str]) -> str:
    """Resolve the ``--ir-scaffold`` mode to a canonical value.

    Path A (closed-world) is now the default. ``on`` routes
    ``self.builder.X(...)`` and ``ir.Y(...)`` call sites to direct
    native IR lowering instead of ``py_cpy_*`` dispatch; methods not
    yet implemented raise a clear error rather than silently falling
    back. ``off`` remains as an explicit compatibility escape hatch.
    """
    raw = mode
    if raw is None:
        raw = os.environ.get(_IR_SCAFFOLD_MODE_ENV, "")
    normalized = str(raw or "").strip().lower()
    if not normalized or normalized == "auto":
        return "on"
    if normalized in ("on", "true", "yes", "1"):
        return "on"
    if normalized in ("off", "false", "no", "0"):
        return "off"
    raise PyPipelineError(
        "invalid ir scaffold mode "
        f"{raw!r}; expected off, on, or auto"
    )


def _finalize_libpython_mode(
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
        raise PyPipelineError(
            "Python pipeline requires libpython fallback for "
            + context
            + suffix
            + "; rerun with --python-libpython=auto/on or "
            + "PCC_PYTHON_LIBPYTHON=auto/on"
        )
    return detected


def _log(verbose: bool, msg: str) -> None:
    if verbose:
        sys.stderr.write("[pcc.py] " + msg + "\n")


def _join_dotted_parts(parts: list[str]) -> str:
    return _join_strings(parts, ".")


def _join_strings(parts: list[str], sep: str) -> str:
    if not parts:
        return ""
    out = parts[0]
    i = 1
    while i < len(parts):
        out += sep + parts[i]
        i += 1
    return out


def _first_string(items: list[str]) -> str:
    return items[0]


def _module_name_from_src(src_path: str) -> str:
    base = str(os.path.basename(src_path))
    if base.endswith(".py"):
        base = base[:-3]
    if not base:
        return "<module>"

    abs_path = str(os.path.abspath(src_path))
    parent_dir = str(os.path.dirname(abs_path))
    if base == "__main__":
        init_py = str(os.path.join(parent_dir, "__init__.py"))
        package_name = str(os.path.basename(parent_dir))
        if package_name and os.path.isfile(init_py):
            return _join_dotted_parts([package_name, "__main__"])

    pkg_parts = []
    cur_dir = parent_dir
    while cur_dir:
        init_py = str(os.path.join(cur_dir, "__init__.py"))
        if not os.path.isfile(init_py):
            break
        pkg_parts.append(str(os.path.basename(cur_dir)))
        parent = str(os.path.dirname(cur_dir))
        if parent == cur_dir:
            break
        cur_dir = parent

    if not pkg_parts:
        return base

    ordered_parts = []
    i = len(pkg_parts) - 1
    while i >= 0:
        ordered_parts.append(pkg_parts[i])
        i -= 1
    if base == "__init__":
        return _join_dotted_parts(ordered_parts)
    return _join_dotted_parts(ordered_parts + [base])


def _module_root_from_src(src_path: str, module_name: str) -> str:
    abs_path = str(os.path.abspath(src_path))
    cur_dir = str(os.path.dirname(abs_path))
    parts = module_name.split(".")
    up = len(parts) if os.path.basename(abs_path) == "__init__.py" else max(
        0, len(parts) - 1,
    )
    i = 0
    while i < up:
        parent = str(os.path.dirname(cur_dir))
        if parent == cur_dir:
            break
        cur_dir = parent
        i += 1
    return cur_dir


def _package_parts_for_module(src_path: str, module_name: str) -> list[str]:
    parts = module_name.split(".")
    if os.path.basename(src_path) == "__init__.py":
        return parts
    return parts[:-1]


def _resolve_module_src(root_dir: str, dotted_name: str) -> Optional[str]:
    parts = dotted_name.split(".")
    py_path = str(os.path.join(root_dir, *parts)) + ".py"
    if os.path.isfile(py_path):
        return py_path
    init_path = str(os.path.join(root_dir, *parts, "__init__.py"))
    if os.path.isfile(init_path):
        return init_path
    return None


def _iter_source_import_from_specs(source: str, *, top_level_only: bool) -> list[tuple[str, list[str]]]:
    """Return ``[(module_spec, [imported_name...]), ...]`` from source text.

    Keep this intentionally narrow: package-closure discovery only needs
    textual ``from ... import ...`` statements, not full Python AST
    fidelity. Avoiding CPython AST objects here keeps the compiled
    bootstrap path away from fragile runtime attribute walks.
    """
    specs: list[tuple[str, list[str]]] = []
    pending: Optional[str] = None
    paren_depth = 0

    def _flush(stmt: str) -> None:
        stmt = stmt.strip()
        if not stmt.startswith("from "):
            return
        rest = stmt[5:]
        split_token = " import "
        split_idx = rest.find(split_token)
        if split_idx < 0:
            return
        module_spec = rest[:split_idx].strip()
        names_spec = rest[split_idx + len(split_token):].strip()
        if not module_spec:
            return
        if "#" in names_spec:
            names_spec = names_spec.split("#", 1)[0].strip()
        if names_spec.startswith("(") and names_spec.endswith(")"):
            names_spec = names_spec[1:-1].strip()
        imported_names = []
        for raw_name in names_spec.split(","):
            raw_name = raw_name.strip()
            if not raw_name or raw_name == "*":
                continue
            if " as " in raw_name:
                raw_name = raw_name.split(" as ", 1)[0].strip()
            imported_names.append(raw_name)
        if imported_names:
            specs.append((module_spec, imported_names))

    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        if pending is None:
            if not stripped:
                continue
            if top_level_only and raw_line[:1].isspace():
                continue
            if not stripped.startswith("from "):
                continue
            pending = stripped
            paren_depth = stripped.count("(") - stripped.count(")")
            if paren_depth <= 0 and not stripped.endswith("\\"):
                _flush(pending)
                pending = None
        else:
            if "#" in stripped:
                stripped = stripped.split("#", 1)[0].rstrip()
            pending += " " + stripped
            paren_depth += stripped.count("(") - stripped.count(")")
            if paren_depth <= 0 and not stripped.endswith("\\"):
                _flush(pending)
                pending = None

    if pending is not None:
        _flush(pending)
    return specs


def _package_import_targets(
    src_path: str,
    mod_name: str,
    *,
    root_dir: Optional[str] = None,
    top_level_only: bool = False,
    include_relative: bool = True,
    include_same_package_absolute: bool = True,
) -> list[tuple[str, str]]:
    """Return package-local import targets for ``src_path``.

    This includes:
    - relative imports such as ``from .foo import bar``
    - absolute same-package imports such as ``from pcc.cli_core import cli_main``

    The second form is kept intentionally narrow: only imports whose module path
    starts with the current package root are considered, so stdlib / third-party
    absolute imports still stay out of the native source closure.
    """
    if "." not in mod_name:
        return []
    if root_dir is None:
        root_dir = _module_root_from_src(src_path, mod_name)

    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()

    current_pkg = _package_parts_for_module(src_path, mod_name)
    package_root = mod_name.split(".")[0]
    targets: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    import_specs = _iter_source_import_from_specs(
        source, top_level_only=top_level_only,
    )
    for module_spec, imported_names in import_specs:
        candidate_mods: list[str] = []
        level = 0
        while level < len(module_spec) and module_spec[level] == ".":
            level += 1
        module_name = module_spec[level:]
        if level > 0 and include_relative:
            up = level - 1
            if up > len(current_pkg):
                continue
            base_pkg = current_pkg[: len(current_pkg) - up]
            if module_name:
                candidate_mods.append(_join_dotted_parts(base_pkg + module_name.split(".")))
                for imported_name in imported_names:
                    candidate_mods.append(
                        _join_dotted_parts(
                            base_pkg
                            + module_name.split(".")
                            + imported_name.split(".")
                        )
                    )
            else:
                for imported_name in imported_names:
                    candidate_mods.append(_join_dotted_parts(base_pkg + imported_name.split(".")))
        elif module_name and include_same_package_absolute:
            mod_parts = module_name.split(".")
            if mod_parts and mod_parts[0] == package_root:
                candidate_mods.append(module_name)
                for imported_name in imported_names:
                    candidate_mods.append(_join_dotted_parts(mod_parts + imported_name.split(".")))
        for target_mod in candidate_mods:
            if not target_mod or target_mod in seen_targets:
                continue
            target_src = _resolve_module_src(root_dir, target_mod)
            if target_src is None:
                continue
            seen_targets.add(target_mod)
            targets.append((target_src, target_mod))
    return targets


def _collect_relative_module_closure(
    entry_src: str,
    *,
    include_same_package_absolute: bool = False,
    recurse_same_package_absolute: bool = False,
) -> tuple[list[str], list[str]]:
    """Return ``([src...], [module_name...])`` for a package entry plus
    recursively imported relative siblings and, optionally, a one-hop
    same-package absolute-import leaf set."""
    entry_src = str(os.path.abspath(entry_src))
    entry_mod = _module_name_from_src(entry_src)
    if "." not in entry_mod:
        return [entry_src], [entry_mod]

    root_dir = _module_root_from_src(entry_src, entry_mod)
    ordered_srcs = [entry_src]
    ordered_mods = [entry_mod]
    seen = {entry_mod: entry_src}
    queue = [(entry_src, entry_mod)]

    if include_same_package_absolute:
        for target_src, target_mod in _package_import_targets(
            entry_src,
            entry_mod,
            root_dir=root_dir,
            include_relative=False,
            include_same_package_absolute=True,
        ):
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            if recurse_same_package_absolute:
                queue.append((target_src, target_mod))

    while queue:
        src_path, mod_name = queue.pop(0)
        for target_src, target_mod in _package_import_targets(
            src_path,
            mod_name,
            root_dir=root_dir,
            include_relative=True,
            include_same_package_absolute=recurse_same_package_absolute,
        ):
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)
            queue.append((target_src, target_mod))

    return ordered_srcs, ordered_mods


def _collect_multi_source_relative_closure(
    src_paths: list[str],
    module_names: list[str],
    *,
    recursive_stdlib: bool = False,
) -> tuple[list[str], list[str]]:
    """Return explicit multi-file sources plus one-hop relative-import
    siblings referenced by those explicit sources.

    When ``recursive_stdlib=True`` (Issue 11.B.1), also pulls in any
    pure-Python stdlib module that's transitively imported by the seed
    set, allowing them to be compiled natively rather than routed
    through ``py_cpy_import``. Modules whose source can't be located,
    aren't ``.py`` files (C extensions / built-ins), or fail pcc's
    parser silently fall back to the dynamic path."""
    ordered_srcs = [str(os.path.abspath(p)) for p in src_paths]
    ordered_mods = [str(m) for m in module_names]
    seen = {
        mod_name: src_path
        for src_path, mod_name in zip(ordered_srcs, ordered_mods)
    }
    seed_items = list(zip(ordered_srcs, ordered_mods))

    for src_path, mod_name in seed_items:
        if "." not in mod_name:
            continue
        root_dir = _module_root_from_src(src_path, mod_name)
        for target_src, target_mod in _package_import_targets(
            src_path,
            mod_name,
            root_dir=root_dir,
            include_relative=True,
            include_same_package_absolute=False,
        ):
            if target_mod in seen:
                continue
            target_src = str(os.path.abspath(target_src))
            seen[target_mod] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(target_mod)

    if recursive_stdlib:
        _expand_recursive_stdlib(ordered_srcs, ordered_mods, seen)

    return ordered_srcs, ordered_mods


def _filter_ir_scaffold_closure(
    src_paths: list[str],
    module_names: list[str],
    *,
    ir_scaffold_mode: str,
) -> tuple[list[str], list[str]]:
    """Map ON-mode IR scaffold imports to their real link provider.

    ``from pcc.llvm_capi.compat import ir`` is a compile-time scaffold
    import in ON mode. The linked stage binary needs definitions from
    ``pcc.llvm_capi.ir`` for the emitted ``user_pcc_llvm_capi_ir_*``
    calls, but it does not need ``compat.py`` or the LLVM-C/JIT
    ``binding.py`` module. Keeping those in the source closure is what
    drags libpython back into the self backend path.
    """
    if ir_scaffold_mode != "on":
        return src_paths, module_names
    skip = {"pcc.llvm_capi.compat", "pcc.llvm_capi.binding"}
    saw_compat = False
    out_srcs: list[str] = []
    out_mods: list[str] = []
    seen: set[str] = set()
    root_dir = None

    def add_ir_provider() -> None:
        if "pcc.llvm_capi.ir" in seen or root_dir is None:
            return
        ir_src = _resolve_module_src(root_dir, "pcc.llvm_capi.ir")
        if ir_src is None:
            return
        out_srcs.append(str(os.path.abspath(ir_src)))
        out_mods.append("pcc.llvm_capi.ir")
        seen.add("pcc.llvm_capi.ir")

    for src, mod in zip(src_paths, module_names):
        if root_dir is None and (mod == "pcc" or mod.startswith("pcc.")):
            root_dir = _module_root_from_src(src, mod)
        if mod == "pcc.llvm_capi.compat":
            saw_compat = True
            add_ir_provider()
            continue
        if mod in skip:
            continue
        if mod in seen:
            continue
        out_srcs.append(src)
        out_mods.append(mod)
        seen.add(mod)
    if saw_compat:
        add_ir_provider()
    return out_srcs, out_mods


def _locate_stdlib_module_source(mod_name: str) -> Optional[str]:
    """Resolve ``mod_name`` to a ``.py`` source path for the recursive
    stdlib walker (Issue 11.C.1).

    Search order:
    1. ``pcc/py_stdlib/<name>.py`` — first-class pcc-native stdlib
       ports used by normal CPython spellings such as ``import string``.
    2. ``pcc/stdlib/<name>.py`` — legacy port registry kept for older
       struct/_float_bits tests.
    3. CPython's stdlib via an external ``python3`` find-spec probe.

    Returns ``None`` for built-ins, C extensions, or modules that
    can't be located.
    """
    repo_pcc_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _port_candidates(root: str) -> list[str]:
        rel = mod_name.replace(".", os.sep)
        return [
            os.path.join(root, f"{rel}.py"),
            os.path.join(root, rel, "__init__.py"),
            # Legacy flat dotted filename form.
            os.path.join(root, f"{mod_name}.py"),
        ]

    pcc_py_stdlib_dir = os.path.join(repo_pcc_dir, "py_stdlib")
    for pcc_port in _port_candidates(pcc_py_stdlib_dir):
        if os.path.isfile(pcc_port):
            return pcc_port
    pcc_stdlib_dir = os.path.join(
        repo_pcc_dir,
        "stdlib",
    )
    for pcc_port in _port_candidates(pcc_stdlib_dir):
        if os.path.isfile(pcc_port):
            return pcc_port
    try:
        py_cmd = str(os.environ.get("PCC_HOST_PYTHON", "") or "python3").strip()
        probe = (
            "import importlib.util,sys; "
            "spec=importlib.util.find_spec(sys.argv[1]); "
            "origin='' if spec is None or spec.origin is None else spec.origin; "
            "print(origin)"
        )
        origin = subprocess.check_output(
            [py_cmd, "-c", probe, mod_name],
            text=True,
        ).strip()
    except Exception:
        return None
    if origin == "" or origin == "built-in":
        return None
    if not origin.endswith(".py"):
        return None
    return origin


def _native_stdlib_root_for_path(path: str) -> Optional[str]:
    repo_pcc_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    normalized = os.path.abspath(path)
    for dirname in ("py_stdlib", "stdlib"):
        root = os.path.abspath(os.path.join(repo_pcc_dir, dirname))
        try:
            common = os.path.commonpath([root, normalized])
        except ValueError:
            continue
        if common == root:
            return root
    return None


def _classify_python_import(
    mod_name: str,
    *,
    native_modules: Optional[set[str]] = None,
) -> str:
    """Classify an import without changing the public CPython spelling.

    Return values are deliberately stable strings so tests and bootstrap
    diagnostics can assert the routing boundary:

    - ``compile_time_only``
    - ``native_user_module``
    - ``builtin_native_dispatch``
    - ``native_stdlib``
    - ``cpython_fallback``
    """
    if not mod_name:
        return "cpython_fallback"
    top = mod_name.split(".")[0]
    if top in _COMPILE_TIME_ONLY_IMPORT_MODULES:
        return "compile_time_only"
    if native_modules is not None and mod_name in native_modules:
        return "native_user_module"
    if mod_name in _NATIVE_BUILTIN_IMPORTS or top in _NATIVE_BUILTIN_IMPORTS:
        return "builtin_native_dispatch"
    located = _locate_stdlib_module_source(mod_name)
    if located is not None and _native_stdlib_root_for_path(located) is not None:
        return "native_stdlib"
    return "cpython_fallback"


def _source_uses_native_stdlib(src_path: str) -> bool:
    for mod_name in _stdlib_absolute_imports_in(src_path):
        if _classify_python_import(mod_name) == "native_stdlib":
            return True
    return False


def _stdlib_absolute_imports_in(src_path: str) -> list[str]:
    """Return absolute imports found anywhere in a source file."""
    from .py_ast import Import as _Import
    from .py_ast import ImportFrom as _ImportFrom
    from ..parse.py_lift import parse_and_lift

    with open(src_path, "r", encoding="utf-8") as f:
        text = f.read()
    try:
        ast_mod = parse_and_lift(text, src_path, "<scan>")
    except Exception:
        return []
    out: list[str] = []

    def _walk(stmts):
        for stmt in stmts:
            if isinstance(stmt, _Import):
                for mod_name, _ in stmt.names:
                    out.append(mod_name)
            elif isinstance(stmt, _ImportFrom):
                if stmt.module is not None and stmt.level == 0:
                    # `from X import a, b` where every name is in
                    # _COMPILE_TIME_ONLY_IMPORT_FROMS[X] is a compile-time
                    # macro / decorator import (e.g. `from dataclasses import
                    # dataclass, field`). Don't treat it as evidence that the
                    # source uses module X at runtime — otherwise the native
                    # stdlib closure pulls in pcc/py_stdlib/X.py and forces
                    # libpython through any py_cpy_* fallbacks in that port.
                    compile_only = _COMPILE_TIME_ONLY_IMPORT_FROMS.get(
                        stmt.module
                    )
                    if compile_only is not None and all(
                        alias_name in compile_only for alias_name, _as_name in stmt.names
                    ):
                        pass
                    else:
                        out.append(stmt.module)
            for slot in (
                "body", "orelse", "finally_body", "else_body", "handlers",
            ):
                sub = getattr(stmt, slot, None)
                if sub is not None and isinstance(sub, tuple):
                    _walk(sub)

    _walk(ast_mod.body)
    return out


def _stdlib_module_compiles(src_path: str, mod_name: str) -> bool:
    """Fail-soft recursive-stdlib codegen probe."""
    from .type_infer import infer_module
    from .codegen.layer1 import L1CodeGen
    from ..parse.py_lift import parse_and_lift

    try:
        with open(src_path, "r", encoding="utf-8") as f:
            text = f.read()
        ast_mod = parse_and_lift(text, src_path, mod_name)
        typed = infer_module(ast_mod)
        cg = L1CodeGen(typed)
        cg.generate(typed)
        return True
    except Exception:
        return False


def _expand_recursive_stdlib(
    ordered_srcs: list[str],
    ordered_mods: list[str],
    seen: dict[str, str],
) -> None:
    """Issue 11.B.1: pull pure-Python stdlib modules transitively into
    the native compile set. In-place: appends to ordered_srcs /
    ordered_mods.

    Walks the seed sources, looks up each absolute non-relative,
    non-whitelisted import via ``importlib.util.find_spec``. A module
    is added to the closure only if pcc can BOTH parse AND codegen
    it (Issue 11.B.2 — fail-soft). Modules that parse but fail
    codegen are excluded; their importers fall back to
    ``py_cpy_import`` for them.
    """
    queue = list(ordered_mods)
    failures: set[str] = set()

    while queue:
        cur_mod = queue.pop(0)
        cur_src = seen.get(cur_mod)
        if cur_src is None:
            continue
        for import_name in _stdlib_absolute_imports_in(cur_src):
            top = import_name.split(".")[0]
            if (
                import_name in seen
                or import_name in failures
                or top in _COMPILE_TIME_ONLY_IMPORT_MODULES
                or top in _NATIVE_BUILTIN_IMPORTS
                or import_name in _SCAFFOLD_IMPORT_MODULES
            ):
                continue
            # Skip pcc internals UNLESS we're pulling pcc.stdlib (the
            # port registry — Issue 11.C.1). Other pcc.* modules are
            # already handled by the relative-import walker for the
            # bootstrap closure.
            if top == "pcc" and not import_name.startswith("pcc.stdlib."):
                continue
            target_src = _locate_stdlib_module_source(import_name)
            if target_src is None:
                continue
            if not _stdlib_module_compiles(target_src, import_name):
                # Issue 11.B.2: parse-OK but codegen-FAIL. Fall back to
                # py_cpy_import for this module by NOT adding it to
                # the native closure.
                failures.add(import_name)
                continue
            seen[import_name] = target_src
            ordered_srcs.append(target_src)
            ordered_mods.append(import_name)
            queue.append(import_name)


def _relative_import_targets(
    src_path: str,
    mod_name: str,
    *,
    root_dir: Optional[str] = None,
    top_level_only: bool = False,
) -> list[tuple[str, str]]:
    """Return package-local import targets for init ordering."""
    return _package_import_targets(
        src_path,
        mod_name,
        root_dir=root_dir,
        top_level_only=top_level_only,
    )


def _order_module_inits(
    src_paths: list[str],
    module_names: list[str],
    entry_module: str,
) -> list[str]:
    """Return a dependency-first order for sibling module top-inits."""
    if len(module_names) <= 1:
        return []
    module_to_src = {
        mod_name: str(os.path.abspath(src_path))
        for src_path, mod_name in zip(src_paths, module_names)
    }
    module_set = set(module_names)
    dep_cache: dict[str, list[str]] = {}
    ordered: list[str] = []
    visiting: set[str] = set()
    emitted: set[str] = set()

    def deps_for(mod_name: str) -> list[str]:
        cached = dep_cache.get(mod_name)
        if cached is not None:
            return cached
        src_path = module_to_src[mod_name]
        deps: list[str] = []
        for _dep_src, dep_mod in _relative_import_targets(
            src_path, mod_name, top_level_only=True,
        ):
            resolved_dep = dep_mod
            if (
                dep_mod == "pcc.llvm_capi.compat"
                and dep_mod not in module_set
                and "pcc.llvm_capi.ir" in module_set
            ):
                # ON-mode scaffold filtering replaces compat with the real
                # pcc.llvm_capi.ir provider. Preserve the top-init dependency
                # so IR type class objects exist before runtime_abi/layer1 use
                # VoidType(), IntType(), FunctionType(), etc.
                resolved_dep = "pcc.llvm_capi.ir"
            if resolved_dep in module_set and resolved_dep != mod_name:
                deps.append(resolved_dep)
        dep_cache[mod_name] = deps
        return deps

    def visit(mod_name: str) -> None:
        if mod_name in emitted:
            return
        if mod_name in visiting:
            return
        visiting.add(mod_name)
        for dep_mod in deps_for(mod_name):
            if dep_mod != entry_module:
                visit(dep_mod)
        visiting.remove(mod_name)
        emitted.add(mod_name)
        if mod_name != entry_module:
            ordered.append(mod_name)

    visit(entry_module)
    for mod_name in module_names:
        if mod_name != entry_module:
            visit(mod_name)
    return ordered


def _export_param_types(args):
    """Return normalized runtime param types for cross-module exports.

    Multi-file extern declarations only need the lowered runtime
    signature shape. Treat missing annotations as DynType and skip the
    bare ``*`` separator, matching codegen's own parameter handling.
    """
    param_tys = []
    for a in args:
        if a.name == "":
            continue
        param_tys.append(
            encode_type(a.annotation)
            if a.annotation is not None
            else ("dyn",)
        )
    return param_tys


def _export_return_type(ret_ty):
    if ret_ty is None:
        from .py_ast import DynType

        return encode_type(DynType(name="dyn"))
    return encode_type(ret_ty)


def _decorator_name(dec):
    from .py_ast import Attr, Call, Name

    if isinstance(dec, Call):
        return _decorator_name(dec.func)
    if isinstance(dec, Name):
        return dec.ident
    if isinstance(dec, Attr):
        base = _decorator_name(dec.obj)
        if base:
            return base + "." + dec.name
    return None


def _class_is_dataclass(cd) -> bool:
    for dec in cd.decorators:
        name = _decorator_name(dec)
        if name in ("dataclass", "dataclasses.dataclass"):
            return True
    return False


def _export_call_sig(args):
    sig = []
    for a in args:
        sig.append({
            "name": a.name,
            "kind": a.kind,
            "annotation": encode_type(a.annotation),
            "default": a.default,
            "has_default": getattr(a, "has_default", a.default is not None),
        })
    return tuple(sig)


def _runtime_archive_stale(archive: str) -> bool:
    if not os.path.isfile(archive):
        return True
    stamp = _runtime_archive_target_stamp(archive)
    if not os.path.isfile(stamp):
        return True
    try:
        with open(stamp, "r", encoding="utf-8") as f:
            if f.read().strip() != _runtime_archive_target_id():
                return True
    except OSError:
        return True
    archive_mtime = os.path.getmtime(archive)
    header = os.path.join(_PY_RUNTIME_DIR, "include", "py_runtime.h")
    if os.path.isfile(header) and os.path.getmtime(header) > archive_mtime:
        return True
    src_dir = os.path.join(_PY_RUNTIME_DIR, "src")
    if os.path.isdir(src_dir):
        for name in os.listdir(src_dir):
            if not name.endswith(".c"):
                continue
            path = os.path.join(src_dir, name)
            if os.path.isfile(path) and os.path.getmtime(path) > archive_mtime:
                return True
    archive_base = str(os.path.basename(archive))
    if archive_base in (
        "libpy_runtime_pcc_py.a",
        "libpy_runtime_pcc_py_libpython.a",
    ):
        py_dir = os.path.join(_PY_RUNTIME_DIR, "py")
        if os.path.isdir(py_dir):
            for name in os.listdir(py_dir):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(py_dir, name)
                if (
                    os.path.isfile(path)
                    and os.path.getmtime(path) > archive_mtime
                ):
                    return True
        if archive_base == "libpy_runtime_pcc_py_libpython.a":
            base_archive = _PY_RUNTIME_ARCHIVE_PCC_PY
            if (
                os.path.isfile(base_archive)
                and os.path.getmtime(base_archive) > archive_mtime
            ):
                return True
    makefile = os.path.join(_PY_RUNTIME_DIR, "Makefile")
    return os.path.isfile(makefile) and os.path.getmtime(makefile) > archive_mtime


def _runtime_archive_target_stamp(archive: str) -> str:
    return str(archive) + ".target"


def _runtime_archive_target_id() -> str:
    import platform

    machine = platform.machine().lower()
    if machine in ("amd64", "x64"):
        machine = "x86_64"
    return f"{sys.platform}:{machine}:{_host_target_triple_for_self_backend()}"


def _write_runtime_archive_target_stamp(archive: str) -> None:
    try:
        with open(_runtime_archive_target_stamp(archive), "w", encoding="utf-8") as f:
            f.write(_runtime_archive_target_id() + "\n")
    except OSError:
        pass


def _runtime_cc_mode() -> str:
    """Return the selected runtime-compiler mode ('cc' or 'pcc').

    Controlled by $PCC_RUNTIME_CC (Phase 2 of the runtime self-host
    plan). Default is now 'pcc' for no-libpython Python builds: the
    bootstrap-safe path should use the pcc-emitted runtime archive.
    Set PCC_RUNTIME_CC=cc explicitly for the host-cc oracle archive.
    """
    value = str(os.environ.get(_PY_RUNTIME_CC_ENV, "") or "").strip().lower()
    if value in ("cc", "c", "host"):
        return "cc"
    if value in ("pcc", "self"):
        return "pcc"
    return "pcc"


def _runtime_high_mode() -> str:
    """Return the selected runtime-high source ('c' or 'py').

    Controlled by $PCC_RUNTIME_HIGH (Phase 4 of the runtime self-host
    plan). Default is now 'py' for no-libpython Python builds.
    'c' = runtime-high modules compiled from C, kept for the pcc-C
    oracle path. 'py' = runtime-high modules compiled from pcc-Python
    ports.
    """
    value = str(os.environ.get(_PY_RUNTIME_HIGH_ENV, "") or "").strip().lower()
    if value in ("c", "cc"):
        return "c"
    if value in ("py", "python"):
        return "py"
    return "py"


def _ensure_runtime(
    verbose: bool, *, needs_libpython: bool = False,
) -> Optional[str]:
    """Locate (and optionally build) the required runtime archive.

    Returns the archive path chosen for linking. When the existence
    probe fails we still return that path after warning so the final
    clang invocation can surface a concrete missing-file/link error
    instead of silently omitting the runtime archive.
    """
    cc_mode = _runtime_cc_mode()
    high_mode = _runtime_high_mode()
    if cc_mode == "pcc":
        # Bootstrap-safe default: pcc-emitted runtime archive with
        # pcc-Python runtime-high modules. PCC_RUNTIME_HIGH=c remains
        # available for the pcc-C oracle path. When CPython fallback is
        # still needed, keep the pcc-Python archive as the base and add
        # only the py_libpython compatibility bridge.
        if high_mode == "py":
            archive = (
                _PY_RUNTIME_ARCHIVE_PCC_PY_LIBPYTHON
                if needs_libpython else
                _PY_RUNTIME_ARCHIVE_PCC_PY
            )
        elif needs_libpython:
            archive = _PY_RUNTIME_ARCHIVE_LIBPYTHON
        else:
            archive = _PY_RUNTIME_ARCHIVE_PCC
    else:
        archive = (
            _PY_RUNTIME_ARCHIVE_LIBPYTHON
            if needs_libpython else _PY_RUNTIME_ARCHIVE
        )
    debug = bool(str(os.environ.get("PCC_DEBUG_RUNTIME", "")).strip())
    if debug:
        _log(True, "[runtime] _PY_RUNTIME_DIR=" + _PY_RUNTIME_DIR)
        _log(True, "[runtime] archive=" + str(archive))
        _log(True, "[runtime] makefile=" + os.path.join(_PY_RUNTIME_DIR, "Makefile"))
        _log(True, "[runtime] needs_libpython=" + str(needs_libpython))
        _log(True, "[runtime] cc_mode=" + str(cc_mode))
        _log(True, "[runtime] high_mode=" + str(high_mode))
        _log(True, "[runtime] archive_exists=" + str(os.path.isfile(archive)))
        if os.path.isfile(archive):
            _log(True, "[runtime] archive_stale=" + str(_runtime_archive_stale(archive)))
        _log(True, "[runtime] makefile_exists=" + str(os.path.isfile(os.path.join(_PY_RUNTIME_DIR, "Makefile"))))

    if os.path.isfile(archive) and not _runtime_archive_stale(archive):
        _log(verbose, "runtime archive: " + archive)
        return archive

    makefile = os.path.join(_PY_RUNTIME_DIR, "Makefile")
    if debug:
        try:
            with open("/tmp/pcc_runtime_debug_probe.txt", "a", encoding="utf-8") as f:
                f.write("[probe] makefile=" + makefile + " exists=" + str(os.path.isfile(makefile)) + " archive=" + archive + " exists=" + str(os.path.isfile(archive)) + "\n")
        except Exception:
            pass
    if os.path.isfile(makefile):
        make_cmd = ["make", "-B", "-C", _PY_RUNTIME_DIR]
        if cc_mode == "pcc":
            if high_mode == "py":
                make_cmd.append(
                    "libpy_runtime_pcc_py_libpython.a"
                    if needs_libpython else
                    "libpy_runtime_pcc_py.a"
                )
            else:
                if needs_libpython:
                    make_cmd.extend(
                        [
                            "PCC_WITH_LIBPYTHON=1",
                            "LIB=libpy_runtime_libpython.a",
                            "OBJDIR=build_libpython",
                        ]
                    )
                else:
                    make_cmd.append("libpy_runtime_pcc.a")
            pcc_bin = _resolve_pcc_binary()
            if pcc_bin and high_mode == "py":
                make_cmd.append(f"PCC={pcc_bin}")
                make_cmd.append(f"PYTHON={sys.executable}")
        elif needs_libpython:
            make_cmd.extend(
                [
                    "PCC_WITH_LIBPYTHON=1",
                    "LIB=libpy_runtime_libpython.a",
                    "OBJDIR=build_libpython",
                ]
            )
        _log(verbose, "building runtime: " + _join_strings(make_cmd, " "))
        try:
            subprocess.run(
                make_cmd,
                check=True,
                capture_output=not verbose,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(
                f"warning: failed to build py_runtime ({e}); "
                "final link may fail on undefined py_* symbols",
                file=sys.stderr,
            )
            return None
        if os.path.isfile(archive):
            _write_runtime_archive_target_stamp(archive)
            _log(verbose, "runtime archive: " + archive)
            return archive

    print(
        "warning: " + archive + " not found; "
        "final link may fail on undefined py_* symbols",
        file=sys.stderr,
    )
    return archive


def _resolve_pcc_binary() -> Optional[str]:
    """Locate the pcc CLI binary for PCC_RUNTIME_CC=pcc builds."""
    env_override = str(os.environ.get("PCC_BINARY", "") or "").strip()
    if env_override:
        return env_override
    # When a compiled pcc binary is building a nested runtime archive,
    # sys.executable still describes the host Python used during stage0,
    # while sys.argv[0] points at the stage binary that must self-compile
    # the runtime modules. Prefer it when it is an executable path rather
    # than a Python source entry point.
    argv0 = str(sys.argv[0] if len(sys.argv) > 0 else "" or "").strip()
    argv0_base = os.path.basename(argv0)
    if argv0 and argv0_base.startswith("pcc") and not argv0.endswith(".py"):
        argv0_path = os.path.abspath(argv0)
        if os.path.isfile(argv0_path) and os.access(argv0_path, os.X_OK):
            return argv0_path
    # Prefer the pcc installed alongside the running Python (uv/venv).
    candidate = os.path.join(
        os.path.dirname(sys.executable), "pcc",
    )
    if os.path.isfile(candidate):
        return candidate
    found = shutil.which("pcc")
    return found


def _emit_ll(ir_text, out_ll_path, verbose: bool) -> None:
    ir_text = str(ir_text)
    out_ll_path = str(out_ll_path)
    if verbose:
        _log(
            verbose,
            "writing LLVM IR to " + out_ll_path
            + " (" + str(len(ir_text)) + " bytes)",
        )
    with open(out_ll_path, "w") as f:
        f.write(ir_text)


def _resolve_python_ir_pass_names(raw: Optional[str] = None) -> list[str]:
    if raw is None:
        raw = os.environ.get(_PYTHON_IR_PASSES_ENV)
        if raw is None or not str(raw).strip():
            return list(_PYTHON_IR_PASS_PRESETS["default"])
    normalized = str(raw or "").strip().lower()
    if normalized in ("off", "false", "no", "0"):
        return []
    if not normalized:
        return list(_PYTHON_IR_PASS_PRESETS["default"])
    if normalized in ("on", "true", "yes", "1"):
        return list(_PYTHON_IR_PASS_PRESETS["default"])

    pass_names: list[str] = []
    for token in normalized.split(","):
        name = token.strip()
        if not name:
            continue
        preset = _PYTHON_IR_PASS_PRESETS.get(name)
        if preset is not None:
            for preset_name in preset:
                if preset_name not in pass_names:
                    pass_names.append(preset_name)
            continue
        if name not in pass_names:
            pass_names.append(name)
    return pass_names


def _apply_python_ir_pass_pipeline(
    ir_text: str,
    *,
    module_name: str,
    verbose: bool = False,
) -> str:
    pass_names = _resolve_python_ir_pass_names()
    if not pass_names:
        return str(ir_text)

    if verbose:
        _log(
            verbose,
            "python IR passes[" + module_name + "]: "
            + _join_strings(pass_names, ", "),
        )
    host_code = (
        "import sys\n"
        "from pcc.py_frontend.ir_pass_pipeline import run_python_ir_pass_pipeline\n"
        "module_name, pass_csv, ir_path, out_path = sys.argv[1:5]\n"
        "pass_names = tuple(name.strip() for name in pass_csv.split(',') "
        "if name.strip())\n"
        "with open(ir_path, 'r', encoding='utf-8') as f:\n"
        "    ir_text = f.read()\n"
        "out = run_python_ir_pass_pipeline(\n"
        "    ir_text, pass_names=pass_names, module_name=module_name,\n"
        ")\n"
        "with open(out_path, 'w', encoding='utf-8') as f:\n"
        "    f.write(out)\n"
    )
    with tempfile.TemporaryDirectory(prefix="pcc_py_ir_passes_") as tmp:
        ir_path = str(os.path.join(tmp, "input.ll"))
        out_path = str(os.path.join(tmp, "output.ll"))
        with open(ir_path, "w", encoding="utf-8") as f:
            f.write(str(ir_text))
        cmd = [
            _host_python_command(),
            "-c",
            host_code,
            module_name,
            _join_strings(pass_names, ","),
            ir_path,
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True)
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            detail = f" (exit {e.returncode})" if hasattr(e, "returncode") else ""
            raise PyPipelineError(
                "Python IR pass pipeline failed for module "
                f"{module_name!r}{detail}"
            ) from e
        with open(out_path, "r", encoding="utf-8") as f:
            return f.read()


def _link_with_clang(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
) -> None:
    """Link one or more ``.ll`` files into a native executable."""
    clang = str(os.environ.get("CC", "") or "").strip() or "clang"
    ll_paths = [str(p) for p in ll_paths]
    out_path = str(out_path)
    if runtime_archive is not None:
        runtime_archive = str(runtime_archive)
    # Runtime exceptions are return-code-based now (see py_exc.c);
    # libc++/libc++abi are no longer linked. libm stays for fp math.
    cmd = [clang, *ll_paths, "-o", out_path, "-lm"]
    if sys.platform == "darwin":
        # Stable bootstrap compare on Mach-O: clang injects a fresh
        # LC_UUID by default, which makes pcc2/pcc3 differ even when
        # the linked inputs are otherwise identical.
        cmd.append("-Wl,-no_uuid")
    if runtime_archive is not None:
        # Put the archive after the .ll inputs so the linker pulls
        # its symbols in once the user objects have declared them.
        insert_at = 1 + len(ll_paths)
        cmd.insert(insert_at, runtime_archive)
    if needs_libpython:
        ldflags_env = str(os.environ.get("PCC_PYTHON_LDFLAGS", "")).strip()
        if ldflags_env:
            cmd.extend(shlex.split(ldflags_env))
        else:
            config_cmd = _resolve_python_config_command()
            try:
                out = str(subprocess.check_output(
                    [config_cmd, "--ldflags", "--embed"],
                    text=True,
                ).strip())
                cmd.extend(out.split())
            except (FileNotFoundError, subprocess.CalledProcessError) as e:
                raise PyPipelineError(
                    f"{config_cmd} required for import-using programs: {e}"
                ) from e
    if verbose:
        _log(verbose, "link: " + _join_strings(cmd, " "))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError as e:
        raise PyPipelineError(
            f"{clang} not found on PATH; cannot link Python frontend output"
        ) from e
    except subprocess.CalledProcessError as e:
        raise PyPipelineError(f"clang link failed (exit {e.returncode})") from e


def _host_target_triple_for_self_backend() -> str:
    cc = str(os.environ.get("CC", "") or "").strip() or "cc"
    try:
        return str(
            subprocess.check_output(
                [cc, "-dumpmachine"],
                text=True,
            ).strip()
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        if sys.platform == "darwin":
            import platform

            machine = platform.machine().lower()
            if machine == "aarch64":
                machine = "arm64"
            return f"{machine}-apple-darwin{platform.release()}"
        if sys.platform.startswith("linux"):
            import platform

            machine = platform.machine().lower()
            if machine in ("amd64", "x64"):
                machine = "x86_64"
            return f"{machine}-unknown-linux-gnu"
        return "unknown-unknown-unknown"


def _self_backend_ir_text(ir_text: str) -> str:
    ir_text = str(ir_text)
    placeholder = 'target triple = "unknown-unknown-unknown"'
    if placeholder in ir_text:
        return ir_text.replace(
            placeholder,
            'target triple = "' + _host_target_triple_for_self_backend() + '"',
            1,
        )
    if 'target triple = "' not in ir_text:
        return (
            'target triple = "' + _host_target_triple_for_self_backend() + '"\n'
            + ir_text
        )
    return ir_text


def _host_python_command() -> str:
    configured = str(os.environ.get("PCC_HOST_PYTHON", "") or "").strip()
    if configured:
        return configured
    cwd_python3 = str(os.path.join(os.getcwd(), ".venv", "bin", "python3"))
    if os.path.isfile(cwd_python3):
        return cwd_python3
    cwd_python = str(os.path.join(os.getcwd(), ".venv", "bin", "python"))
    if os.path.isfile(cwd_python):
        return cwd_python
    return "python3"


def _emit_self_asm_via_host_python(
    ir_text: str, tmp_dir: str, index: int,
) -> tuple[str, str]:
    ir_path = str(os.path.join(tmp_dir, f"self_backend_input_{index}.ll"))
    with open(ir_path, "w", encoding="utf-8") as f:
        f.write(ir_text)
    host_py = _host_python_command()
    try:
        out = str(
            subprocess.check_output(
                [host_py, "-c", _SELF_BACKEND_HOST_CODE, ir_path],
                text=True,
            )
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise PyPipelineError(
            f"self backend native emission failed: {e}"
        ) from e
    lines = out.splitlines()
    if not lines:
        raise PyPipelineError(
            "self backend native emission failed: host emitter produced "
            "no output"
        )
    target_id = lines[0]
    asm_text = "\n".join(lines[1:])
    return target_id, asm_text


def _self_backend_jobs(n_modules: int) -> int:
    n_modules = int(n_modules)
    if n_modules <= 1:
        return 1
    raw = str(os.environ.get(_SELF_BACKEND_JOBS_ENV, "") or "").strip()
    if raw:
        try:
            jobs = int(raw)
        except ValueError:
            jobs = 1
        return max(1, min(n_modules, jobs))
    cpu_count = os.cpu_count() or 1
    return max(1, min(n_modules, cpu_count))


def _platform_link_flags() -> list[str]:
    if sys.platform.startswith("linux"):
        return ["-no-pie", "-Wl,--build-id=none", "-s"]
    return []


def _append_libpython_link_flags(cmd: list[str]) -> None:
    ldflags_env = str(os.environ.get("PCC_PYTHON_LDFLAGS", "")).strip()
    if ldflags_env:
        cmd.extend(shlex.split(ldflags_env))
        return
    config_cmd = _resolve_python_config_command()
    try:
        out = str(
            subprocess.check_output(
                [config_cmd, "--ldflags", "--embed"],
                text=True,
            ).strip()
        )
        cmd.extend(out.split())
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise PyPipelineError(
            f"{config_cmd} required for import-using programs: {e}"
        ) from e


def _link_with_self_backend(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    needs_libpython: bool = False,
) -> None:
    """Lower ``.ll`` files through the self backend and link native asm."""
    ll_paths = [str(p) for p in ll_paths]
    out_path = str(out_path)
    if runtime_archive is not None:
        runtime_archive = str(runtime_archive)

    cc = str(os.environ.get("CC", "") or "").strip() or "cc"
    with tempfile.TemporaryDirectory(prefix="pcc_py_self_") as tmp:
        asm_modules = []
        needs_subsections_via_symbols = False
        host_results = [None] * len(ll_paths)
        for idx, ll_path in enumerate(ll_paths):
            with open(ll_path, "r", encoding="utf-8") as f:
                ir_text = _self_backend_ir_text(f.read())
            host_results[idx] = _emit_self_asm_via_host_python(
                ir_text, tmp, idx,
            )

        for result in host_results:
            if result is None:
                raise PyPipelineError(
                    "self backend native emission failed: missing module "
                    "result"
                )
            target_id, asm_text = result
            asm_lines = asm_text.splitlines()
            if asm_lines and asm_lines[-1] == ".subsections_via_symbols":
                asm_lines = asm_lines[:-1]
            if target_id == "self-aarch64-darwin-v0":
                needs_subsections_via_symbols = True
            asm_modules.append("\n".join(asm_lines).strip())

        asm_text = "\n\n".join(fragment for fragment in asm_modules if fragment)
        if needs_subsections_via_symbols:
            asm_text += "\n.subsections_via_symbols\n"

        asm_path = str(os.path.join(tmp, "self_backend.s"))
        with open(asm_path, "w", encoding="utf-8") as f:
            f.write(asm_text)
        cmd = [cc, asm_path, "-o", out_path, "-lm"]
        if sys.platform == "darwin":
            cmd.append("-Wl,-no_uuid")
            if needs_subsections_via_symbols:
                cmd.append("-Wl,-dead_strip")
        cmd.extend(_platform_link_flags())
        if runtime_archive is not None:
            cmd.insert(2, runtime_archive)
        if needs_libpython:
            _append_libpython_link_flags(cmd)
        _log(verbose, "self link: " + _join_strings(cmd, " "))
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError as e:
            raise PyPipelineError(
                f"{cc} not found on PATH; cannot link Python frontend output"
            ) from e
        except subprocess.CalledProcessError as e:
            raise PyPipelineError(
                f"self backend link failed (exit {e.returncode})"
            ) from e


def _link_native(
    ll_paths,
    out_path: str,
    runtime_archive: Optional[str],
    verbose: bool,
    *,
    backend,
    needs_libpython: bool = False,
) -> None:
    kind = _native_backend_kind(backend)
    if kind == "llvm":
        _link_with_clang(
            ll_paths,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
        )
        return
    if kind == "self":
        _link_with_self_backend(
            ll_paths,
            out_path,
            runtime_archive,
            verbose,
            needs_libpython=needs_libpython,
        )
        return
    raise PyPipelineError(f"unsupported Python native backend: {kind}")


def _module_needs_libpython(
    ast_module,
    native_modules=None,
    ir_scaffold_mode: str = "off",
) -> bool:
    """Scan the parsed AST for any ``import`` statement; if present the
    link step must pull in libpython because codegen will emit
    ``py_cpy_*`` calls. Walks both module scope and function bodies.

    ``native_modules`` is an optional iterable of dotted names that
    are being compiled natively in the same multi-file invocation —
    imports of these are routed to extern symbols (no CPython path).
    """
    from .py_ast import Import as _Import
    from .py_ast import ImportFrom as _ImportFrom
    from .py_ast import Name as _Name
    from .py_ast import Type as _Type

    native_set = set(native_modules or ())
    cur_mod = getattr(ast_module, "name", "") or ""
    cur_parts = cur_mod.split(".") if cur_mod else []

    def _field_names(obj):
        fields = getattr(obj, "__dataclass_fields__", None)
        if fields is None:
            return ()
        return tuple(fields.keys())

    def _field_value(obj, field_name, default=None):
        return getattr(obj, field_name, default)

    def _name_used_at_runtime(stmts, ident: str) -> bool:
        annotation_slots = {"annotation", "return_ty"}

        def walk(x) -> bool:
            if x is None:
                return False
            if isinstance(x, _Type):
                return False
            if isinstance(x, (_Import, _ImportFrom)):
                return False
            if isinstance(x, tuple):
                for it in x:
                    if walk(it):
                        return True
                return False
            if isinstance(x, _Name):
                return x.ident == ident
            for slot in _field_names(x):
                if slot in annotation_slots:
                    continue
                if walk(_field_value(x, slot, None)):
                    return True
            return False

        return walk(stmts)

    def _is_scaffold_module(module_name: str | None) -> bool:
        if module_name == "pcc.llvm_capi.compat":
            return (
                ir_scaffold_mode == "on"
                or cur_mod == "pcc.py_frontend.codegen.runtime_abi"
            )
        return module_name in _SCAFFOLD_IMPORT_MODULES

    def _resolve_relative(module, level):
        if not level:
            return module or ""
        if level > len(cur_parts):
            return module or ""
        base = cur_parts[: len(cur_parts) - level]
        if module:
            return ".".join(base + [module])
        return ".".join(base)

    def _walk(stmts) -> bool:
        for stmt in stmts:
            if isinstance(stmt, _ImportFrom):
                if _is_scaffold_module(stmt.module):
                    continue
                if (
                    stmt.module is not None
                    and stmt.module.split(".")[0] in _COMPILE_TIME_ONLY_IMPORT_MODULES
                ):
                    continue
                resolved = _resolve_relative(
                    stmt.module, stmt.level or 0,
                )
                compile_only_froms = _COMPILE_TIME_ONLY_IMPORT_FROMS.get(
                    resolved
                )
                if compile_only_froms is not None:
                    remaining_names = [
                        alias_name
                        for alias_name, as_name in stmt.names
                        if alias_name not in compile_only_froms
                    ]
                    if not remaining_names:
                        continue
                allowed_froms = _NATIVE_IMPORT_FROMS.get(resolved)
                if allowed_froms is not None:
                    if all(
                        alias_name in allowed_froms
                        for alias_name, _as_name in stmt.names
                    ):
                        continue
                if resolved in native_set:
                    continue
                return True
            if isinstance(stmt, _Import):
                remaining = []
                for m, as_name in stmt.names:
                    local_name = as_name or m.split(".")[0]
                    if (
                        m.split(".")[0] in _COMPILE_TIME_ONLY_IMPORT_MODULES
                        or m in _NATIVE_BUILTIN_IMPORTS
                        or m in native_set
                    ):
                        continue
                    if (
                        m in _ANNOTATION_ONLY_IMPORT_MODULES
                        and not _name_used_at_runtime(ast_module.body, local_name)
                    ):
                        continue
                    remaining.append(m)
                if not remaining:
                    continue
                return True
            # Only descend into the body / handler / else branches of
            # statements we know carry a list of sub-stmts. Using
            # Explicit attribute access — each AST node has a known
            # shape; no need for dynamic getattr. Drop through when a
            # field is missing.
            body = stmt.body if hasattr(stmt, "body") else None
            if body and _walk(body):
                return True
            else_body = stmt.else_body if hasattr(stmt, "else_body") else None
            if else_body and _walk(else_body):
                return True
            finally_body = stmt.finally_body if hasattr(stmt, "finally_body") else None
            if finally_body and _walk(finally_body):
                return True
            handlers = stmt.handlers if hasattr(stmt, "handlers") else None
            if handlers:
                for h in handlers:
                    h_body = h.body if hasattr(h, "body") else ()
                    if _walk(h_body):
                        return True
        return False

    return _walk(ast_module.body)


def _resolve_python_config_command() -> str:
    config_env = str(os.environ.get("PCC_PYTHON_CONFIG", "")).strip()
    if config_env:
        return config_env

    try:
        import sysconfig as _sysconfig

        bindir = str(_sysconfig.get_config_var("BINDIR") or "").strip()
        ldversion = str(
            _sysconfig.get_config_var("LDVERSION")
            or _sysconfig.get_config_var("VERSION")
            or ""
        ).strip()
        candidates = []
        if bindir:
            if ldversion:
                candidates.append(
                    str(os.path.join(bindir, f"python{ldversion}-config"))
                )
            candidates.extend(
                [
                    str(os.path.join(bindir, "python3-config")),
                    str(os.path.join(bindir, "python-config")),
                ]
            )
        for candidate in candidates:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    except Exception:
        pass

    return "python3-config"


def _ir_needs_libpython(ir_text: str) -> bool:
    """Return True when IR calls a CPython helper that requires the
    libpython-backed runtime archive.

    Generated ``main`` only calls ``py_cpy_main_exitcode`` when the
    compile is already known to need the CPython fallback, so any
    remaining ``py_cpy_*`` call site means the libpython archive is
    required.
    """
    if "@py_cpy_" not in ir_text:
        return False
    for line in ir_text.splitlines():
        if "@py_cpy_" not in line:
            continue
        stripped = line.lstrip()
        if stripped.startswith("call ") or stripped.startswith("tail call "):
            return True
        if " = call " in line or " = tail call " in line:
            return True
    return False


def compile_python(
    src_path: str,
    out_path: str,
    *,
    verbose: bool = False,
    emit_llvm_only: bool = False,
    libpython_mode: Optional[str] = None,
    ir_scaffold_mode: Optional[str] = None,
    backend: Optional[str] = None,
    recursive_stdlib: bool = False,
    python_library: bool = False,
) -> None:
    """Compile a single ``.py`` file to a native executable.

    Parameters
    ----------
    src_path:
        Path to the input ``.py`` source file.
    out_path:
        Output path. When ``emit_llvm_only`` is False this is the
        native executable; when True it is the ``.ll`` file.
    verbose:
        If True, print each pipeline step and timing info to stderr.
    emit_llvm_only:
        If True, stop after writing LLVM IR (used by ``--emit-llvm``).
    backend:
        Native emission backend for executable output. ``llvm`` keeps
        the historical clang ``.ll`` path; ``self`` lowers ``.ll``
        through the in-repo asm backend before linking.
    python_library:
        Emit a library module object shape: no program ``@main`` is
        synthesized, but module init/top-init functions remain available
        for an embedding entrypoint to call. This is intended for
        pcc-Python runtime archives and must be paired with
        ``emit_llvm_only``.
    """
    if python_library and not emit_llvm_only:
        raise PyPipelineError(
            "python_library mode requires emit_llvm_only=True"
        )
    # Imports are deferred so that modules still under construction by
    # sibling agents don't break ``pcc --help`` or ``.c`` compilation.
    libpython_mode = _resolve_libpython_mode(libpython_mode)
    ir_scaffold_mode = _resolve_ir_scaffold_mode(ir_scaffold_mode)

    try:
        from .type_infer import infer_module as _infer_module
        from .codegen.layer1 import L1CodeGen as _L1CodeGen
    except ImportError as e:
        raise PyPipelineError(
            f"Python frontend module not available: {e}. "
            "The Python pipeline is currently Phase 1 MVP and some "
            "components may still be under construction."
        ) from e

    if not os.path.isfile(src_path):
        raise PyPipelineError(f"input file not found: {src_path}")

    module_name = _module_name_from_src(src_path)
    should_auto_close = (not emit_llvm_only) or module_name.endswith(".__main__")
    auto_srcs, auto_mods = (
        _collect_relative_module_closure(
            src_path,
            include_same_package_absolute=(
                module_name.endswith(".__main__")
            ),
            recurse_same_package_absolute=(libpython_mode == "off"),
        )
        if should_auto_close else
        ([str(os.path.abspath(src_path))], [module_name])
    )
    auto_srcs, auto_mods = _filter_ir_scaffold_closure(
        auto_srcs, auto_mods, ir_scaffold_mode=ir_scaffold_mode,
    )
    effective_recursive_stdlib = recursive_stdlib
    if (
        not effective_recursive_stdlib
        and libpython_mode == "off"
        and not python_library
        and _source_uses_native_stdlib(src_path)
    ):
        effective_recursive_stdlib = True
    # Issue 11.B.1.2: when recursive_stdlib is on, force the multi-file
    # path so _expand_recursive_stdlib has a chance to pull pure-Python
    # stdlib into the native compile. The multi-file path also
    # populates _native_module_exports which lets _emit_import skip
    # py_cpy_import for natively-compiled modules.
    if effective_recursive_stdlib and len(auto_srcs) == 1:
        entry = _first_string(auto_mods)
        compile_python_multi(
            auto_srcs,
            out_path,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            entry_module=entry,
            module_names=auto_mods,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            recursive_stdlib=True,
        )
        return
    if len(auto_srcs) > 1:
        if python_library:
            raise PyPipelineError(
                "python_library mode only supports a single Python source"
            )
        if verbose:
            _log(
                verbose,
                "auto multi-file package compile: "
                + _join_strings(auto_mods, ", "),
            )
        entry = _first_string(auto_mods)
        compile_python_multi(
            auto_srcs,
            out_path,
            verbose=verbose,
            emit_llvm_only=emit_llvm_only,
            entry_module=entry,
            module_names=auto_mods,
            libpython_mode=libpython_mode,
            ir_scaffold_mode=ir_scaffold_mode,
            backend=backend,
            recursive_stdlib=effective_recursive_stdlib,
        )
        return

    if verbose:
        _log(verbose, "reading " + src_path)
    with open(src_path, "r", encoding="utf-8") as f:
        source = f.read()

    _log(verbose, "parse")
    # pcc.parse.py_parse + pcc.parse.py_lift is the bootstrap-safe
    # parser path. The previous CPython-ast escape hatch kept a
    # libpython import edge alive in the compiled pipeline, so the
    # self-host path no longer emits it.
    from ..parse.py_lift import parse_and_lift as _parse_and_lift
    _log(verbose, "parse")
    ast_mod = _parse_and_lift(
        source, src_path, _module_name_from_src(src_path),
    )

    ast_needs_libpython = _module_needs_libpython(
        ast_mod,
        ir_scaffold_mode=ir_scaffold_mode,
    )

    _log(verbose, "type_infer")
    typed_mod = _infer_module(ast_mod)

    _log(verbose, "codegen (layer1)")
    codegen = _L1CodeGen(
        typed_mod,
        emit_cpy_main_exitcode=(
            libpython_mode == "on"
            or (libpython_mode == "auto" and ast_needs_libpython)
        ),
        ir_scaffold_mode=ir_scaffold_mode,
    )
    if python_library:
        codegen._skip_program_main = True
    ir_text = codegen.generate(typed_mod)
    if not isinstance(ir_text, str):
        # Some codegen implementations may return a module-like object
        # with __str__; coerce defensively.
        ir_text = str(ir_text)
    ir_text = _apply_python_ir_pass_pipeline(
        ir_text, module_name=module_name, verbose=verbose,
    )

    if emit_llvm_only:
        # out_path is a .ll path; just write it and return.
        _emit_ll(ir_text, out_path, verbose)
        return

    needs_libpython = ast_needs_libpython
    reasons = []
    if needs_libpython:
        reasons.append("imports still lower through CPython fallback")
    # Fallback: scan the generated IR for direct call sites into the
    # libpython shim (``py_cpy_*``). Codegen emits these for DynType
    # method dispatch, ``hasattr`` fallback, ``x.__copy__()`` and
    # similar even when the source has no explicit ``import``. Using
    # ``\bcall`` rather than a plain text search avoids triggering on
    # the ``declare external`` stubs emitted unconditionally for all
    # runtime helpers.
    if _ir_needs_libpython(ir_text):
        needs_libpython = True
        reasons.append("generated IR still calls py_cpy_* helpers")
    needs_libpython = _finalize_libpython_mode(
        detected=needs_libpython,
        mode=libpython_mode,
        context=str(src_path),
        reasons=reasons,
    )
    native_backend = _resolve_native_backend(backend)
    if verbose:
        _log(verbose, "native backend: " + str(native_backend))

    # Write IR to a temp .ll, link with clang + runtime, produce exe.
    with tempfile.TemporaryDirectory(prefix="pcc_py_") as tmp:
        ll_name = str(os.path.basename(out_path)) + ".ll"
        ll_path = str(os.path.join(tmp, ll_name))
        _emit_ll(ir_text, ll_path, verbose)
        runtime = _ensure_runtime(
            verbose, needs_libpython=needs_libpython,
        )
        _link_native(
            [ll_path], out_path, runtime, verbose,
            backend=native_backend,
            needs_libpython=needs_libpython,
        )
    if verbose:
        _log(verbose, "wrote executable: " + out_path)


def compile_python_multi(
    src_paths,
    out_path: str,
    *,
    verbose: bool = False,
    emit_llvm_only: bool = False,
    entry_module: Optional[str] = None,
    module_names=None,
    libpython_mode: Optional[str] = None,
    ir_scaffold_mode: Optional[str] = None,
    backend: Optional[str] = None,
    recursive_stdlib: bool = False,
) -> None:
    """Compile multiple ``.py`` files into a single native executable.

    This is the infrastructure step for #138.5 three-stage
    bootstrap. Each source file is parsed, type-inferred, and
    lowered to LLVM IR independently; the resulting ``.ll`` files
    are handed to clang together so that cross-module symbol
    references — declared as ``external`` by each module's codegen
    — are resolved at link time.

    Parameters
    ----------
    src_paths:
        Ordered list of ``.py`` files. The first entry provides
        the native executable's ``main`` entry point (the one
        that pcc synthesises to call top-level module code).
    module_names:
        Optional parallel list of dotted module names. Defaults
        to the filename stem. The names influence the
        ``user_<module>_<fn>`` symbol mangling so two files can
        define unrelated ``main`` functions without colliding.
    entry_module:
        Dotted module name whose top-level ``main()`` is the
        executable entry. Defaults to the first file's module.

    The multi-compile API **does not** yet rewrite cross-module
    imports to extern references — step 2 of the spike plan
    (``docs/plans/multi-file-compile-spike.md``). Until that
    lands, imports between passed source files still route through
    ``py_cpy_import`` and the link pulls libpython. Single-file
    callers should keep using :func:`compile_python`.
    """
    if not src_paths:
        raise PyPipelineError(
            "compile_python_multi requires at least one source file"
        )
    libpython_mode = _resolve_libpython_mode(libpython_mode)
    ir_scaffold_mode = _resolve_ir_scaffold_mode(ir_scaffold_mode)
    src_paths = list(src_paths)
    if module_names is None:
        module_names = [_module_name_from_src(p) for p in src_paths]
    if len(module_names) != len(src_paths):
        raise PyPipelineError(
            "module_names length must match src_paths length"
        )
    if len(src_paths) == 1:
        src_paths, module_names = _collect_multi_source_relative_closure(
            src_paths, list(module_names),
            recursive_stdlib=recursive_stdlib,
        )
    elif recursive_stdlib:
        src_paths, module_names = _collect_multi_source_relative_closure(
            src_paths, list(module_names),
            recursive_stdlib=True,
        )
    src_paths, module_names = _filter_ir_scaffold_closure(
        src_paths, list(module_names), ir_scaffold_mode=ir_scaffold_mode,
    )

    try:
        from .type_infer import infer_module as _infer_module
        from .codegen.layer1 import L1CodeGen as _L1CodeGen
    except ImportError as e:
        raise PyPipelineError(
            f"Python frontend module not available: {e}"
        ) from e

    any_needs_libpython = False
    libpython_modules = []
    module_ir_texts = []

    # Decide which module is the entry (emits ``@main``). Default:
    # first source file in the list.
    if entry_module is None:
        entry_module = module_names[0]
    if entry_module not in module_names:
        raise PyPipelineError(
            f"entry_module {entry_module!r} not among module_names "
            f"{module_names!r}"
        )
    # Sibling modules whose top-level code the entry must run before
    # its own body. Use dependency order instead of caller order so a
    # child module never initializes before its imported base module.
    sibling_inits = _order_module_inits(src_paths, module_names, entry_module)

    # Pre-pass: parse every module and collect exported top-level
    # FuncDefs per module. This table is given to each codegen so
    # ``from .sibling import fn`` can declare ``fn`` as an extern
    # with the correct signature instead of routing through the
    # py_cpy_import path (which would pull libpython).
    from . import py_ast as _py_ast
    native_exports = {}
    # Per-mod: {name: {kind: 'function'|'class', ...}}

    # Pre-pass 1: parse all modules and extract exports from top-level
    # FuncDef/ClassDef annotations without running type inference.
    # This way each module's inference (pre-pass 2) sees the
    # cross-module export table built from the raw annotations.
    for src, mod_name in zip(src_paths, module_names):
        if verbose:
            _log(verbose, "exports " + mod_name)
        with open(src, "r", encoding="utf-8") as f:
            source = f.read()
        from ..parse.py_lift import parse_and_lift as _parse_and_lift
        ast_mod = _parse_and_lift(source, src, mod_name)
        exports = {}
        class_field_defs = {}
        box_int_abi = not (
            mod_name == "pcc"
            or mod_name.startswith("pcc.")
            or mod_name == "bootstrap"
            or mod_name.startswith("bootstrap.")
        )
        for s in ast_mod.body:
            if isinstance(s, _py_ast.FuncDef):
                exports[s.name] = {
                    "kind": "function",
                    "return_ty": _export_return_type(s.return_ty),
                    "param_types": _export_param_types(s.args),
                    "call_sig": _export_call_sig(s.args),
                    "box_int_abi": box_int_abi,
                }
            elif isinstance(s, _py_ast.Assign):
                if len(s.targets) != 1 or not isinstance(s.targets[0], _py_ast.Name):
                    continue
                target_name = s.targets[0].ident
                value = s.value
                if isinstance(value, _py_ast.StrLit):
                    exports[target_name] = {
                        "kind": "constant",
                        "value_kind": "str",
                        "value": value.value,
                    }
                elif isinstance(value, _py_ast.IntLit):
                    exports[target_name] = {
                        "kind": "constant",
                        "value_kind": "int",
                        "value": int(value.value),
                    }
                elif isinstance(value, _py_ast.BoolLit):
                    exports[target_name] = {
                        "kind": "constant",
                        "value_kind": "bool",
                        "value": bool(value.value),
                    }
                elif isinstance(value, _py_ast.NoneLit):
                    exports[target_name] = {
                        "kind": "constant",
                        "value_kind": "none",
                        "value": None,
                    }
            elif isinstance(s, _py_ast.ClassDef):
                # Mirror ClassLowering's instance-field schema. Plain
                # class-body ``x: T`` entries are class attributes unless the
                # class is a dataclass; treating them as instance fields here
                # shifts cross-module slot indexes away from the real local
                # layout.
                class_is_dataclass = _class_is_dataclass(s)
                field_names = []
                field_defs = []
                for base_expr in s.bases:
                    if not isinstance(base_expr, _py_ast.Name):
                        continue
                    for inherited in class_field_defs.get(base_expr.ident, ()):
                        if inherited["name"] not in field_names:
                            field_names.append(inherited["name"])
                        # The inherited field descriptor is immutable for this
                        # pre-pass. Reuse it directly instead of relying on
                        # ``dict(existing_dict)`` during bootstrap; pcc1's
                        # native path must not corrupt dataclass field names
                        # before synthetic __init__ metadata is exported.
                        field_defs.append(inherited)
                methods = []
                for body_stmt in s.body:
                    if isinstance(body_stmt, _py_ast.Assign):
                        if class_is_dataclass:
                            for t in body_stmt.targets:
                                if (
                                    isinstance(t, _py_ast.Name)
                                    and t.ident not in field_names
                                ):
                                    field_names.append(t.ident)
                                if (
                                    isinstance(t, _py_ast.Name)
                                    and body_stmt.annotation is not None
                                ):
                                    field_defs.append({
                                        "name": t.ident,
                                        "annotation": body_stmt.annotation,
                                        "default": body_stmt.value,
                                        "has_default": body_stmt.value is not None,
                                    })
                    elif isinstance(body_stmt, _py_ast.FuncDef):
                        kind = "instance"
                        for dec in body_stmt.decorators:
                            if isinstance(dec, _py_ast.Name):
                                if dec.ident == "staticmethod":
                                    kind = "static"
                                elif dec.ident == "classmethod":
                                    kind = "classmethod"
                                elif dec.ident == "property":
                                    kind = "property_getter"
                        methods.append({
                            "name": body_stmt.name,
                            "kind": kind,
                            "return_ty": _export_return_type(
                                body_stmt.return_ty
                            ),
                            "param_types": _export_param_types(
                                body_stmt.args
                            ),
                            "call_sig": _export_call_sig(body_stmt.args),
                            "box_int_abi": box_int_abi,
                        })
                        for init_stmt in body_stmt.body:
                            if isinstance(init_stmt, _py_ast.Assign):
                                for t in init_stmt.targets:
                                    if (
                                        isinstance(t, _py_ast.Attr)
                                        and isinstance(t.obj, _py_ast.Name)
                                        and t.obj.ident == "self"
                                        and t.name not in field_names
                                    ):
                                        field_names.append(t.name)
                class_field_defs[s.name] = tuple(field_defs)
                if (
                    class_is_dataclass
                    and field_defs
                    and not any(m["name"] == "__init__" for m in methods)
                ):
                    from .py_ast import DynType, NoneType

                    init_sig = [{
                        "name": "self",
                        "kind": "pos",
                        "annotation": None,
                        "default": None,
                        "has_default": False,
                    }]
                    init_param_types = [encode_type(DynType(name="dyn"))]
                    for field in field_defs:
                        init_sig.append({
                            "name": field["name"],
                            "kind": "pos",
                            "annotation": encode_type(field["annotation"]),
                            "default": field["default"],
                            "has_default": field["has_default"],
                        })
                        init_param_types.append(
                            encode_type(field["annotation"] or DynType(name="dyn"))
                        )
                    methods.append({
                        "name": "__init__",
                        "kind": "instance",
                        "return_ty": encode_type(NoneType(name="None")),
                        "param_types": tuple(init_param_types),
                        "call_sig": tuple(init_sig),
                        "box_int_abi": box_int_abi,
                    })
                # Bucket 1: include field type annotations so
                # type_infer can populate ClassType.fields for
                # imported classes. This lets ``fd.body`` access
                # resolve to the field's declared type instead of
                # falling back to DynType.
                field_types_table = []
                for fdef in field_defs:
                    ann = fdef.get("annotation")
                    if ann is not None:
                        field_types_table.append((
                            fdef["name"], encode_type(ann),
                        ))
                base_names = []
                for base in s.bases:
                    if (
                        isinstance(base, _py_ast.Name)
                        and base.ident != "object"
                    ):
                        base_names.append(base.ident)
                exports[s.name] = {
                    "kind": "class",
                    "class_name": s.name,
                    "base_names": base_names,
                    "field_names": tuple(field_names),
                    "field_types": tuple(field_types_table),
                    "methods": tuple(methods),
                    "box_int_abi": box_int_abi,
                }
        native_exports[mod_name] = exports

    # Pre-pass 2 + codegen: re-parse each module so CPython AST /
    # typed-module objects never get parked inside pcc-native
    # list/tuple/dict containers during bootstrap.
    for src, mod_name in zip(src_paths, module_names):
        with open(src, "r", encoding="utf-8") as f:
            source = f.read()
        from ..parse.py_lift import parse_and_lift as _parse_and_lift
        ast_mod = _parse_and_lift(source, src, mod_name)
        external_for_this = {
            k: v for k, v in native_exports.items() if k != mod_name
        }
        if verbose:
            _log(verbose, "type_infer[" + mod_name + "]")
        typed_mod = _infer_module(
            ast_mod, external_exports=external_for_this,
        )
        if verbose:
            _log(verbose, "codegen " + mod_name)
        codegen = _L1CodeGen(
            typed_mod,
            emit_cpy_main_exitcode=(libpython_mode == "on"),
            ir_scaffold_mode=ir_scaffold_mode,
        )
        is_entry = (mod_name == entry_module)
        codegen._skip_program_main = not is_entry
        if is_entry:
            codegen._sibling_module_inits = tuple(sibling_inits)
        # Exclude the current module from the cross-module registry so
        # ``from .sibling import`` within the sibling itself isn't a
        # self-reference.
        codegen._native_module_exports = {
            k: v for k, v in native_exports.items() if k != mod_name
        }
        if verbose:
            _log(verbose, "codegen[" + mod_name + "]")
        ir_text = codegen.generate(typed_mod)
        ir_text = str(ir_text)
        ir_text = _apply_python_ir_pass_pipeline(
            ir_text, module_name=mod_name, verbose=verbose,
        )
        module_ir_texts.append((mod_name, ir_text))
        if _ir_needs_libpython(ir_text):
            any_needs_libpython = True
            if mod_name not in libpython_modules:
                libpython_modules.append(mod_name)

    any_needs_libpython = _finalize_libpython_mode(
        detected=any_needs_libpython,
        mode=libpython_mode,
        context="multi-file compile",
        reasons=(
            ["modules: " + ", ".join(libpython_modules)]
            if libpython_modules else []
        ),
    )
    native_backend = None
    if not emit_llvm_only:
        native_backend = _resolve_native_backend(backend)
        if verbose:
            _log(verbose, "native backend: " + str(native_backend))

    if emit_llvm_only:
        # Concatenate all IR texts with a separator comment so the
        # output is still valid LLVM IR (each module's header lines
        # are duplicated but ``llvm-as`` tolerates redundant
        # target-triple / datalayout directives).
        combined = str("\n\n".join(
            f"; ---- module: {name} ----\n{text}"
            for name, text in module_ir_texts
        ))
        out_path = str(out_path)
        if verbose:
            _log(
                verbose,
                "writing LLVM IR to " + out_path
                + " (" + str(len(combined)) + " bytes)",
            )
        with open(out_path, "w") as f:
            f.write(combined)
        return

    with tempfile.TemporaryDirectory(prefix="pcc_py_multi_") as tmp:
        ll_paths = []
        for mod_name, text in module_ir_texts:
            safe = mod_name.replace(".", "_").replace("-", "_")
            p = str(os.path.join(tmp, safe + ".ll"))
            text = str(text)
            if verbose:
                _log(
                    verbose,
                    "writing LLVM IR to " + p
                    + " (" + str(len(text)) + " bytes)",
                )
            with open(p, "w") as f:
                f.write(text)
            ll_paths.append(p)
        runtime = _ensure_runtime(
            verbose, needs_libpython=any_needs_libpython,
        )
        _link_native(
            ll_paths, out_path, runtime, verbose,
            backend=native_backend,
            needs_libpython=any_needs_libpython,
        )
    if verbose:
        _log(verbose, "wrote executable: " + out_path)
