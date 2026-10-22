"""Filesystem-backed package resources for native compiled modules.

This provider owns the ordinary source-package case: a compiled package has a
real source ``__file__`` and its resource files remain beside ``__init__.py``.
Zip importers, namespace-package merging, custom ``ResourceReader`` objects,
and extraction from opaque loaders are not silently approximated.  Eager
``read_*`` operations are owned; stream-returning ``open*`` operations fail
closed until native file provenance can cross a compiled function boundary.
"""
from __future__ import annotations

import os

from . import import_module


__all__ = [
    "as_file",
    "contents",
    "files",
    "is_resource",
    "open_binary",
    "open_text",
    "path",
    "read_binary",
    "read_text",
]


def _validate_text_options(encoding, errors):
    if encoding is None:
        encoding = "utf-8"
    if errors is None:
        errors = "strict"
    if not isinstance(encoding, str):
        raise TypeError("encoding must be a string")
    normalized_encoding = encoding.lower().replace("_", "-")
    if normalized_encoding not in ("utf-8", "utf8"):
        raise NotImplementedError(
            "filesystem resources currently own only UTF-8 text decoding"
        )
    if errors != "strict":
        raise NotImplementedError(
            "filesystem resources currently own only strict text decoding"
        )
    return encoding, errors


class _ResourcePath:
    """Read-only Traversable for one filesystem package root."""

    def __init__(self, root, resource_path=None):
        self._root = os.path.realpath(str(root))
        if resource_path is None:
            self._path = self._root
        else:
            self._path = os.path.realpath(str(resource_path))
        if os.path.commonpath([self._root, self._path]) != self._root:
            raise ValueError("resource path resolves outside its package")

    def __str__(self):
        return self._path

    def __repr__(self):
        return "_ResourcePath(" + repr(self._path) + ")"

    def __fspath__(self):
        return self._path

    @property
    def name(self):
        return os.path.basename(self._path)

    @property
    def parent(self):
        parent_path = os.path.dirname(self._path)
        if os.path.commonpath([self._root, parent_path]) != self._root:
            return self
        return _ResourcePath(self._root, parent_path)

    def joinpath(self, *descendants):
        candidate = self._path
        for descendant in descendants:
            text = str(descendant)
            if text == "" or os.path.isabs(text):
                raise ValueError("resource names must be non-empty relative paths")
            for component in text.split("/"):
                if component == "" or component == ".":
                    continue
                if component == "..":
                    raise ValueError("resource path may not contain parent traversal")
                candidate = os.path.join(candidate, component)
        return _ResourcePath(self._root, candidate)

    def __truediv__(self, descendant):
        return self.joinpath(descendant)

    def is_file(self):
        return os.path.isfile(self._path)

    def is_dir(self):
        return os.path.isdir(self._path)

    def iterdir(self):
        if not self.is_dir():
            raise NotADirectoryError(self._path)
        return [self.joinpath(name) for name in os.listdir(self._path)]

    def open(self, mode="r", encoding=None, errors=None):
        if mode not in ("r", "rb"):
            raise NotImplementedError(
                "importlib.resources Traversable objects are read-only"
            )
        if mode == "r":
            _validate_text_options(encoding, errors)
        raise NotImplementedError(
            "stream-returning resource APIs require native file provenance; "
            "use read_bytes() or read_text() in pcc-native mode"
        )

    def read_bytes(self):
        # Keep the context expression as a direct builtin ``open`` call.  The
        # native frontend owns that file lifetime shape explicitly; routing it
        # through a user-method return would require a wider dynamic context-
        # manager/file-provenance claim than this provider makes.
        with open(self._path, "rb") as stream:
            return stream.read()

    def read_text(self, encoding="utf-8", errors="strict"):
        _validate_text_options(encoding, errors)
        # The native text-file object currently treats encoding/newline kwargs
        # as compatibility metadata.  Decode bytes here so this provider owns
        # UTF-8 validation and universal-newline behavior rather than silently
        # inheriting that narrower file-runtime contract.
        with open(self._path, "rb") as stream:
            data = stream.read()
        return (
            data.decode("utf-8")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )


class _ResourceContext:
    """No-copy context yielding a reliably consumable filesystem path."""

    def __init__(self, resource):
        self._resource = resource

    def __enter__(self):
        # pcc-native file/path lowering consumes strings directly.  Returning
        # the Traversable would imply a general os.PathLike/__fspath__ runtime
        # dispatch that is not yet owned.
        return str(self._resource)

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def _package_root(package):
    if isinstance(package, str):
        if package == "":
            raise ValueError("Empty module name")
        module = import_module(package)
        package_name = package
    else:
        module = package
        package_name = getattr(module, "__name__", None)
        if not isinstance(package_name, str) or package_name == "":
            raise TypeError("package must be a module name or module object")

    origin = getattr(module, "__file__", None)
    if not isinstance(origin, str) or origin == "":
        raise NotImplementedError(
            "importlib.resources supports only filesystem-backed packages "
            "with a source __file__"
        )
    directory = os.path.dirname(origin)
    filename = os.path.basename(origin)
    if filename == "__init__.py":
        return _ResourcePath(directory)
    if (
        filename.startswith("__init__.")
        and filename.endswith(".pyc")
        and os.path.basename(directory) == "__pycache__"
    ):
        return _ResourcePath(os.path.dirname(directory))
    raise TypeError(repr(package_name) + " is not a package")


def files(package):
    """Return a read-only Traversable rooted at ``package``."""
    return _package_root(package)


def _legacy_resource(package, resource):
    if not isinstance(resource, str):
        raise TypeError("resource name must be a string")
    if (
        resource == ""
        or "/" in resource
        or "\\" in resource
        or resource == "."
        or resource == ".."
    ):
        raise ValueError("resource must be a single relative name")
    return files(package).joinpath(resource)


def as_file(traversable):
    """Expose a materialized filesystem Traversable as a context manager."""
    if not isinstance(traversable, _ResourcePath):
        raise NotImplementedError(
            "as_file extraction is unsupported for non-filesystem Traversables"
        )
    return _ResourceContext(traversable)


def path(package, resource):
    return as_file(_legacy_resource(package, resource))


def open_binary(package, resource):
    _legacy_resource(package, resource)
    raise NotImplementedError(
        "open_binary() cannot expose a reliable cross-module native stream; "
        "use read_binary() in pcc-native mode"
    )


def open_text(package, resource, encoding="utf-8", errors="strict"):
    _legacy_resource(package, resource)
    _validate_text_options(encoding, errors)
    raise NotImplementedError(
        "open_text() cannot expose a reliable cross-module native stream; "
        "use read_text() in pcc-native mode"
    )


def read_binary(package, resource):
    return _legacy_resource(package, resource).read_bytes()


def read_text(package, resource, encoding="utf-8", errors="strict"):
    return _legacy_resource(package, resource).read_text(
        encoding=encoding, errors=errors
    )


def contents(package):
    return [entry.name for entry in files(package).iterdir()]


def is_resource(package, name):
    return _legacy_resource(package, name).is_file()
