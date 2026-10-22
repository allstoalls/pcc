"""Bounded, self-host-safe discovery of an artifact's distribution metadata."""

import os


def _source_wrapper_marker(name: str) -> bool:
    return name in ("PKG-INFO", "pyproject.toml", "setup.py", "setup.cfg")


def _direct_importable_module(name: str) -> bool:
    if name.startswith("."):
        return False
    lower = name.lower()
    return (
        lower.endswith(".py")
        or lower.endswith(".pyi")
        or lower.endswith(".so")
        or lower.endswith(".pyd")
        or lower.endswith(".dll")
        or lower.endswith(".dylib")
    )


def _metadata_members_at_root(names: list[str], prefix: str) -> list[str]:
    paths: list[str] = []
    for original in names:
        name = original
        while name.startswith("./"):
            name = name[2:]
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix) :]
        parts = relative.split("/")
        if relative == "METADATA" or relative == "PKG-INFO":
            paths.append(original)
        elif (
            len(parts) == 2
            and not parts[0].startswith(".")
            and (parts[0].endswith(".dist-info") or parts[0].endswith(".egg-info"))
            and parts[1] in ("METADATA", "PKG-INFO")
        ):
            paths.append(original)
    return paths


def package_metadata_member_paths(names: list[str]) -> list[str]:
    """Apply the same root/src/sdist-wrapper boundary to archive member names."""
    paths = _metadata_members_at_root(names, "")
    paths.extend(_metadata_members_at_root(names, "src/"))
    if paths:
        return paths
    directories: list[str] = []
    for original in names:
        name = original
        while name.startswith("./"):
            name = name[2:]
        if "/" not in name:
            if _source_wrapper_marker(name) or _direct_importable_module(name):
                return paths
            continue
        first = name.split("/", 1)[0]
        if first.startswith(".") or first in (
            "build",
            "dist",
            "venv",
            "env",
            "__pycache__",
            "",
        ):
            continue
        if first not in directories:
            directories.append(first)
    if len(directories) == 1:
        prefix = directories[0] + "/"
        for original in names:
            name = original
            while name.startswith("./"):
                name = name[2:]
            if name.startswith(prefix) and _source_wrapper_marker(name[len(prefix) :]):
                paths.extend(_metadata_members_at_root(names, prefix))
                paths.extend(_metadata_members_at_root(names, prefix + "src/"))
                break
    return paths


def _metadata_at_root(root: str, paths: list[str]) -> None:
    if not os.path.isdir(root):
        return
    for filename in ("METADATA", "PKG-INFO"):
        path = os.path.join(root, filename)
        if os.path.isfile(path) and path not in paths:
            paths.append(path)
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        if not (name.endswith(".dist-info") or name.endswith(".egg-info")):
            continue
        directory = os.path.join(root, name)
        if not os.path.isdir(directory):
            continue
        for filename in ("METADATA", "PKG-INFO"):
            path = os.path.join(directory, filename)
            if os.path.isfile(path) and path not in paths:
                paths.append(path)


def package_metadata_paths(root: str) -> list[str]:
    """Find root/src metadata and one unpacked-sdist wrapper, never a tree walk.

    A development venv, build directory or vendored project does not own the
    artifact's requirements. Bounded locations also avoid symlink recursion.
    """
    paths: list[str] = []
    _metadata_at_root(root, paths)
    _metadata_at_root(os.path.join(root, "src"), paths)
    if paths or not os.path.isdir(root):
        return paths
    directories: list[str] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isfile(path) and (
            _source_wrapper_marker(name) or _direct_importable_module(name)
        ):
            return paths
        if name.startswith(".") or name in (
            "build",
            "dist",
            "venv",
            "env",
            "__pycache__",
        ):
            continue
        if os.path.isdir(path):
            directories.append(path)
    if len(directories) == 1:
        for name in ("PKG-INFO", "pyproject.toml", "setup.py", "setup.cfg"):
            if os.path.isfile(os.path.join(directories[0], name)):
                _metadata_at_root(directories[0], paths)
                _metadata_at_root(os.path.join(directories[0], "src"), paths)
                break
    return paths
