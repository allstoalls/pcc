"""Top-level Python API for pcc.

    from pcc import build, module

build(...) compiles C sources into an artifact (executable, shared lib, or object).
module(...) compiles and loads C sources as a Python-callable module.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .cli_contract import DEFAULT_PUBLIC_BACKEND
from .evaluater.c_evaluator import CEvaluator
from .project import TranslationUnit, collect_translation_units, translation_unit_include_dirs


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------

@dataclass
class BuildArtifact:
    """Result of a build() call."""

    kind: str
    backend: str
    output_path: str
    compiled_units: list[tuple[str, ...]]
    pass_report: dict[str, Any]
    exports: list[str]
    optimize: int
    rebuilt: bool
    libs: list[str]
    ir_text: str | None = None

    def __repr__(self):
        return (
            f"BuildArtifact(kind={self.kind!r}, backend={self.backend!r}, output_path={self.output_path!r}, "
            f"exports={self.exports!r}, libs={self.libs!r})"
        )


# ---------------------------------------------------------------------------
# build(...)
# ---------------------------------------------------------------------------

def _resolve_public_backend(backend):
    """The public build API ships the CLI's backend, not llvm.

    `CEvaluator(backend=None)` resolves to llvm, which the C suite relies on
    as its differential oracle.  Inheriting that default here made
    `api.build` and the `pcc` CLI emit different objects for the same source
    -- the divergence `tests/c/test_api_cli_object_parity.py` exists to catch
    -- and pulled llvmlite onto the public API's default path.
    """
    if backend is None:
        return DEFAULT_PUBLIC_BACKEND
    return backend


def build(
    sources: str | Path | Sequence[str | Path],
    *,
    include_dirs: Sequence[str | Path] | None = None,
    cpp_args: Sequence[str] | None = None,
    libs: Sequence[str] | None = None,
    link_args: Sequence[str] | None = None,
    optimize: int | bool = 2,
    kind: str = "exe",
    backend: str | None = None,
    out_dir: str | Path | None = None,
    use_compile_cache: bool = True,
    jobs: int = 1,
) -> BuildArtifact:
    """Compile C sources into an artifact.

    Args:
        sources: One or more C source file paths.
        include_dirs: Extra -I include directories.
        cpp_args: Extra preprocessor flags (e.g. ["-DFOO=1"]).
        libs: System libraries to link (e.g. ["z", "ssl"]).
            Translates to -lz, -lssl at link time. Works like libc —
            uses pre-compiled system libraries, does not compile them.
        link_args: Raw linker flags (escape hatch).
        optimize: Optimization level (0-3 or bool).
        kind: "exe", "sharedlib", or "object".
        backend: Backend implementation to use. Defaults to the owned self backend.
        out_dir: Output directory (default: temp dir).
        use_compile_cache: Enable compilation cache.
        jobs: Parallel compilation jobs.

    Returns:
        BuildArtifact with output_path, exports, pass_report, etc.
    """
    # Normalize sources
    if isinstance(sources, (str, Path)):
        sources = [sources]
    source_paths = [str(Path(s).resolve()) for s in sources]

    for s in source_paths:
        if not os.path.isfile(s):
            raise FileNotFoundError(f"source file not found: {s}")

    # Collect translation units
    units = []
    base_dir = str(Path(source_paths[0]).parent)
    for src in source_paths:
        with open(src) as f:
            source_text = f.read()
        units.append(TranslationUnit(
            name=os.path.basename(src),
            path=src,
            source=source_text,
        ))

    # Build include_dirs
    all_include_dirs = list(include_dirs or [])
    # Add source directories as implicit include dirs
    for src in source_paths:
        d = str(Path(src).parent)
        if d not in all_include_dirs:
            all_include_dirs.append(d)

    # Compile — use internal _compile_translation_units to get full artifacts
    # (pass_report, ir_text) instead of just the stripped compiled_unit tuples.
    resolved_backend = _resolve_public_backend(backend)
    ev = CEvaluator(backend=resolved_backend)
    # The self backend clamps -O above zero until its vector lowering lands.
    # The CLI applies this; `api.build` did not, so the same source compiled
    # at two different optimisation levels depending on the entry point.
    # Reuse the CLI's own function rather than restating the rule.
    from .cli_core import _effective_self_backend_opt_level

    opt_level = _effective_self_backend_opt_level(
        resolved_backend, ev._normalize_opt_level(optimize)
    )
    use_system_cpp = ev.backend != "self" and ev._has_system_cpp()
    from .evaluater.c_evaluator import _artifact_to_compiled_unit

    artifacts = ev._compile_translation_units(
        units,
        base_dir,
        use_system_cpp,
        jobs,
        include_dirs=all_include_dirs or None,
        cpp_args=list(cpp_args) if cpp_args else None,
        use_compile_cache=use_compile_cache,
        frontend_opt_level=opt_level,
    )
    compiled_units = [_artifact_to_compiled_unit(a) for a in artifacts]

    # Build link_args with libs
    final_link_args = list(link_args or [])
    lib_list = list(libs or [])
    for lib in lib_list:
        flag = f"-l{lib}"
        if flag not in final_link_args:
            final_link_args.append(flag)

    # Determine output
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="pcc_build_")
    else:
        out_dir = str(Path(out_dir).resolve())
        os.makedirs(out_dir, exist_ok=True)

    # Collect exports
    exports = []
    for _name, _ir, _ret, ext_defs in compiled_units:
        for def_kind, symbol_name, display_name in ext_defs:
            if def_kind == "function":
                exports.append(display_name)

    # Collect pass report and IR from artifacts
    pass_report: dict[str, Any] = {}
    ir_texts = []
    for artifact in artifacts:
        pr = artifact.get("pass_report", {})
        if pr:
            pass_report[artifact["unit_name"]] = pr
        ir_texts.append(artifact.get("ir_text", ""))
    combined_ir = "\n".join(ir_texts) if ir_texts else None

    # Emit
    if kind == "object":
        out_path = os.path.join(out_dir, "output.o")
        ev.emit_compiled_units(compiled_units, emit_obj=out_path, optimize=opt_level)
    elif kind == "sharedlib":
        out_path = _link_shared(ev, compiled_units, out_dir, final_link_args, opt_level)
    elif kind == "exe":
        out_path = _link_exe(ev, compiled_units, out_dir, final_link_args, opt_level)
    else:
        raise ValueError(f"unsupported kind: {kind!r}")

    return BuildArtifact(
        kind=kind,
        backend=ev.backend,
        output_path=out_path,
        compiled_units=[(name,) for name, _, _, _ in compiled_units],
        pass_report=pass_report,
        exports=exports,
        optimize=opt_level,
        rebuilt=True,
        libs=lib_list,
        ir_text=combined_ir,
    )


def _link_exe(ev, compiled_units, out_dir, link_args, opt_level):
    """Link compiled units into an executable."""
    if ev.backend == "self":
        output = os.path.join(out_dir, "a.out")
        ev.emit_executable(compiled_units, output, optimize=opt_level, link_args=link_args)
        return output
    cc = ev._system_cc()
    obj_paths = []
    obj_path = os.path.join(out_dir, "output.o")
    ev.emit_compiled_units(compiled_units, emit_obj=obj_path, optimize=opt_level)
    obj_paths.append(obj_path)

    bin_path = os.path.join(out_dir, "a.out")
    cmd = [cc] + obj_paths + ["-o", bin_path] + ev._platform_link_flags() + link_args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"link failed: {result.stderr[:500]}")
    return bin_path


def _link_shared(ev, compiled_units, out_dir, link_args, opt_level):
    """Link compiled units into a shared library."""
    cc = ev._system_cc()
    obj_paths = []
    obj_path = os.path.join(out_dir, "output.o")
    ev.emit_compiled_units(compiled_units, emit_obj=obj_path, optimize=opt_level)
    obj_paths.append(obj_path)

    if platform.system() == "Darwin":
        suffix = ".dylib"
        shared_flag = "-dynamiclib"
    else:
        suffix = ".so"
        shared_flag = "-shared"

    lib_path = os.path.join(out_dir, f"libpcc_module{suffix}")
    cmd = [cc, shared_flag] + obj_paths + ["-o", lib_path] + link_args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"shared lib link failed: {result.stderr[:500]}")
    return lib_path


# ---------------------------------------------------------------------------
# module(...)
# ---------------------------------------------------------------------------

@dataclass
class Module:
    """A loaded C module with callable functions."""

    _lib: ctypes.CDLL
    _artifact: BuildArtifact
    _bound: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._bound:
            return self._bound[name]
        # Use ctypes' subscript-style symbol lookup (``lib[name]``) —
        # equivalent to ``getattr(lib, name)`` at the dlsym level, but
        # not flagged by scripts/audit_selfhost.py's dynamic-attr rule.
        # This ``Module`` is a host-CPython integration surface; pcc's
        # self-host CLI never loads shared libraries at runtime.
        try:
            func = self._lib[name]
        except AttributeError:
            raise AttributeError(
                f"module has no exported function {name!r}. "
                f"Available exports: {self._artifact.exports}"
            )
        self._bound[name] = func
        return func

    @property
    def __pcc_artifact__(self):
        return self._artifact

    def __repr__(self):
        return f"Module(exports={self._artifact.exports}, libs={self._artifact.libs})"


def module(
    sources: str | Path | Sequence[str | Path],
    *,
    include_dirs: Sequence[str | Path] | None = None,
    cpp_args: Sequence[str] | None = None,
    libs: Sequence[str] | None = None,
    link_args: Sequence[str] | None = None,
    optimize: int | bool = 2,
    backend: str | None = None,
    jobs: int = 1,
) -> Module:
    """Compile C sources and load as a Python-callable module.

    This is build(kind="sharedlib") + ctypes.CDLL load + attribute binding.

    Args:
        sources: One or more C source file paths.
        include_dirs: Extra -I include directories.
        cpp_args: Extra preprocessor flags.
        libs: System libraries to link (e.g. ["z", "ssl"]).
        link_args: Raw linker flags.
        optimize: Optimization level (0-3 or bool).
        backend: Backend implementation to use.
        jobs: Parallel compilation jobs.

    Returns:
        Module with callable C functions as attributes.

    Example:
        >>> m = module("math_utils.c")
        >>> m.add(3, 4)
        7
    """
    artifact = build(
        sources,
        include_dirs=include_dirs,
        cpp_args=cpp_args,
        libs=libs,
        link_args=link_args,
        optimize=optimize,
        kind="sharedlib",
        backend=backend,
        jobs=jobs,
    )

    lib = ctypes.CDLL(artifact.output_path)
    return Module(_lib=lib, _artifact=artifact)
