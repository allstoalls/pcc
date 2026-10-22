"""Generic package build-plan consumption for pcc.

This module does not build NumPy or any other package directly. It consumes
package-agnostic build metadata such as compile_commands.json and reports the
work pcc would need to perform or delegate for native extension builds.
"""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from .metadata import inspect_artifact

_C_SUFFIXES = {".c"}
_CXX_SUFFIXES = {".cc", ".cpp", ".cxx", ".c++"}
_FORTRAN_SUFFIXES = {".f", ".for", ".f77", ".f90", ".f95", ".f03", ".f08"}


@dataclass(frozen=True)
class BuildCommand:
    file: str
    command: str
    directory: str | None
    language: str
    compiler: str | None
    output: str | None
    include_dirs: tuple[str, ...]
    defines: tuple[str, ...]
    library_dirs: tuple[str, ...]
    libraries: tuple[str, ...]
    frameworks: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "command": self.command,
            "directory": self.directory,
            "language": self.language,
            "compiler": self.compiler,
            "output": self.output,
            "include_dirs": list(self.include_dirs),
            "defines": list(self.defines),
            "library_dirs": list(self.library_dirs),
            "libraries": list(self.libraries),
            "frameworks": list(self.frameworks),
        }


@dataclass(frozen=True)
class BuildPlan:
    ok: bool
    name: str
    path: str | None
    source_kind: str
    commands: tuple[BuildCommand, ...]
    source_summary: dict[str, int]
    actions: tuple[str, ...]
    diagnostics: tuple[str, ...]
    metadata: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "name": self.name,
            "path": self.path,
            "source_kind": self.source_kind,
            "commands": [command.as_dict() for command in self.commands],
            "source_summary": dict(self.source_summary),
            "actions": list(self.actions),
            "diagnostics": list(self.diagnostics),
            "metadata": self.metadata,
        }


def _language_for_file(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix in _C_SUFFIXES:
        return "c"
    if suffix in _CXX_SUFFIXES:
        return "cxx"
    if suffix in _FORTRAN_SUFFIXES:
        return "fortran"
    return "unknown"


def _consume_joined_flag(
    tokens: list[str], index: int, prefix: str
) -> tuple[str | None, int]:
    token = tokens[index]
    if token == prefix:
        if index + 1 < len(tokens):
            return tokens[index + 1], index + 1
        return None, index
    if token.startswith(prefix) and len(token) > len(prefix):
        return token[len(prefix) :], index
    return None, index


def _parse_build_command(entry: dict[str, object]) -> BuildCommand:
    file_name = str(entry.get("file") or "")
    raw_command = str(entry.get("command") or "")
    directory = str(entry.get("directory") or "") or None
    arguments = entry.get("arguments")
    if isinstance(arguments, list):
        tokens = [str(item) for item in arguments]
        # The structured argv is authoritative and must survive paths/defines
        # containing spaces when consumers later reconstruct it with shlex.
        raw_command = " ".join(shlex.quote(token) for token in tokens)
    else:
        try:
            tokens = shlex.split(raw_command)
        except ValueError:
            tokens = raw_command.split()
    compiler = tokens[0] if tokens else None
    include_dirs: list[str] = []
    defines: list[str] = []
    library_dirs: list[str] = []
    libraries: list[str] = []
    frameworks: list[str] = []
    output: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        value, new_i = _consume_joined_flag(tokens, i, "-I")
        if value is not None:
            include_dirs.append(value)
            i = new_i + 1
            continue
        value, new_i = _consume_joined_flag(tokens, i, "-D")
        if value is not None:
            defines.append(value)
            i = new_i + 1
            continue
        value, new_i = _consume_joined_flag(tokens, i, "-L")
        if value is not None:
            library_dirs.append(value)
            i = new_i + 1
            continue
        value, new_i = _consume_joined_flag(tokens, i, "-l")
        if value is not None:
            libraries.append(value)
            i = new_i + 1
            continue
        if token == "-framework" and i + 1 < len(tokens):
            frameworks.append(tokens[i + 1])
            i += 2
            continue
        value, new_i = _consume_joined_flag(tokens, i, "-o")
        if value is not None:
            output = value
            i = new_i + 1
            continue
        i += 1
    return BuildCommand(
        file=file_name,
        command=raw_command,
        directory=directory,
        language=_language_for_file(file_name),
        compiler=compiler,
        output=output,
        include_dirs=tuple(include_dirs),
        defines=tuple(defines),
        library_dirs=tuple(library_dirs),
        libraries=tuple(libraries),
        frameworks=tuple(frameworks),
    )


def load_compile_commands(path: str | Path) -> tuple[BuildCommand, ...]:
    source = Path(path).expanduser().resolve()
    try:
        entries = json.loads(source.read_text(encoding="utf-8"))
    except Exception:
        return ()
    if not isinstance(entries, list):
        return ()
    commands: list[BuildCommand] = []
    for entry in entries:
        if isinstance(entry, dict):
            commands.append(_parse_build_command(entry))
    return tuple(commands)


def _as_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int, float))]
    if isinstance(value, (str, int, float)):
        return [str(value)]
    return []


def _default_compiler_for_language(language: str) -> str:
    if language == "cxx":
        return "c++"
    if language == "fortran":
        return "gfortran"
    return "cc"


def _meson_intro_targets_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_file():
        return path if path.name == "intro-targets.json" else None
    candidates = (
        path / "meson-info" / "intro-targets.json",
        path / "build" / "meson-info" / "intro-targets.json",
        path
        / "build"
        / "pcc-package"
        / "meson-build"
        / "meson-info"
        / "intro-targets.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_meson_introspection_commands(
    path: str | Path,
    source_root: str | Path | None = None,
) -> tuple[BuildCommand, ...]:
    """Consume Meson introspection target sources as generic compile actions.

    Meson still owns real project configuration. This helper only turns the
    stable source/tool/parameter shape from intro-targets.json into pcc's
    package-agnostic BuildCommand records.
    """
    raw_path = Path(path).expanduser().resolve()
    intro_path = _meson_intro_targets_path(raw_path)
    if intro_path is None:
        return ()
    if source_root is not None:
        root = Path(source_root).expanduser().resolve()
    elif intro_path.parent.name == "meson-info":
        root = intro_path.parent.parent
        if (
            root.name == "meson-build"
            and root.parent.name == "pcc-package"
            and root.parent.parent.name == "build"
            and (root.parent.parent.parent / "meson.build").exists()
        ):
            root = root.parent.parent.parent
        if root.name == "build" and (root.parent / "meson.build").exists():
            root = root.parent
    else:
        root = raw_path if raw_path.is_dir() else intro_path.parent
    try:
        entries = json.loads(intro_path.read_text(encoding="utf-8"))
    except Exception:
        return ()
    if not isinstance(entries, list):
        return ()
    commands: list[BuildCommand] = []
    index = 0
    for target in entries:
        if not isinstance(target, dict):
            continue
        groups = target.get("target_sources")
        if not isinstance(groups, list):
            groups = [target]
        for group in groups:
            if not isinstance(group, dict):
                continue
            language = str(group.get("language") or "")
            compiler_values = _as_string_list(group.get("compiler"))
            parameters = _as_string_list(group.get("parameters"))
            sources = []
            sources.extend(_as_string_list(group.get("sources")))
            sources.extend(_as_string_list(group.get("generated_sources")))
            sources.extend(_as_string_list(group.get("unity_sources")))
            for raw_source in sources:
                source_path = Path(raw_source)
                if not source_path.is_absolute():
                    source_path = root / source_path
                command_language = language or _language_for_file(str(source_path))
                if command_language not in {"c", "cxx", "fortran"}:
                    command_language = _language_for_file(str(source_path))
                if command_language not in {"c", "cxx", "fortran"}:
                    continue
                selected_compiler = compiler_values or [
                    _default_compiler_for_language(command_language)
                ]
                output = (
                    root
                    / "build"
                    / "pcc-package"
                    / "meson"
                    / f"{source_path.stem}_{index}.o"
                )
                command = [
                    *selected_compiler,
                    *parameters,
                    "-c",
                    str(source_path),
                    "-o",
                    str(output),
                ]
                commands.append(
                    _parse_build_command(
                        {
                            "file": str(source_path),
                            "command": " ".join(shlex.quote(part) for part in command),
                            "directory": str(root),
                        }
                    )
                )
                index += 1
    return tuple(commands)


def _compile_commands_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_dir():
        candidates = (
            path / "compile_commands.json",
            path / "build" / "compile_commands.json",
            path / "build" / "pcc-package" / "meson-build" / "compile_commands.json",
        )
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def build_plan_for_artifact(name: str, path: str | Path | None = None) -> BuildPlan:
    raw_path = Path(path).expanduser().resolve() if path is not None else None
    metadata = inspect_artifact(name, raw_path)
    metadata_dict = metadata.as_dict()
    compile_path = _compile_commands_path(raw_path)
    meson_intro_path = _meson_intro_targets_path(raw_path)
    commands = load_compile_commands(compile_path) if compile_path is not None else ()
    if not commands and meson_intro_path is not None:
        commands = load_meson_introspection_commands(meson_intro_path, raw_path)
    summary = {
        "c": sum(1 for command in commands if command.language == "c"),
        "cxx": sum(1 for command in commands if command.language == "cxx"),
        "fortran": sum(1 for command in commands if command.language == "fortran"),
        "unknown": sum(1 for command in commands if command.language == "unknown"),
    }

    actions: list[str] = []
    diagnostics: list[str] = list(metadata.diagnostics)
    if commands:
        if compile_path is not None:
            actions.append("consume_compile_commands")
        elif meson_intro_path is not None:
            actions.append("consume_meson_introspection")
    elif raw_path is not None and raw_path.is_dir():
        diagnostics.append("compile_commands_missing")
    if metadata.generated_c_artifacts:
        actions.append("consume_generated_c")
    if metadata.requires_cython_regeneration:
        diagnostics.append("cython_regeneration_required")
    if metadata.fortran_sources or summary["fortran"]:
        actions.append("delegate_fortran_toolchain")
        diagnostics.append("fortran_toolchain_required")
    libraries = {lib.lower() for command in commands for lib in command.libraries}
    native_fallbacks = set(metadata.native_library_fallbacks)
    if metadata.blas_indicators or "blas" in libraries or "openblas" in libraries:
        actions.append("detect_blas")
        if "blas" not in native_fallbacks:
            diagnostics.append("blas_vendor_required")
    if metadata.lapack_indicators or "lapack" in libraries:
        actions.append("detect_lapack")
        if "lapack" not in native_fallbacks:
            diagnostics.append("lapack_vendor_required")
    if metadata.native_extensions:
        diagnostics.append("prebuilt_native_extension_requires_abi_check")
    if metadata.pyproject_build_backend:
        actions.append("consume_pyproject_build_backend")
    if metadata.meson_build:
        actions.append("consume_meson_plan")
    if meson_intro_path is not None:
        actions.append("consume_meson_introspection")

    return BuildPlan(
        ok=True,
        name=metadata.name,
        path=str(raw_path) if raw_path is not None else None,
        source_kind=metadata.source_kind,
        commands=commands,
        source_summary=summary,
        actions=tuple(dict.fromkeys(actions)),
        diagnostics=tuple(dict.fromkeys(diagnostics)),
        metadata=metadata_dict,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pcc.package build-plan")
    parser.add_argument("name", nargs="?", default="package")
    parser.add_argument("--path", default=None)
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    path = ns.path or (ns.name if Path(ns.name).expanduser().exists() else None)
    name = "" if path == ns.name else ns.name
    plan = build_plan_for_artifact(name, path)
    print(json.dumps(plan.as_dict(), indent=2, sort_keys=True))
    return 0 if plan.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
