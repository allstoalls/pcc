"""Finite :mod:`importlib.machinery` metadata for pcc-native programs.

The executable's linked-module registry, not a runtime Python-code evaluator,
owns importing.  Constants therefore advertise only the canonical pcc-native
source and extension suffixes; in particular, no second entry is presented as
CPython's limited-ABI suffix.  Loader objects expose useful source inspection
but fail closed for source, bytecode, frozen-module, and extension execution.
"""
from __future__ import annotations

import os
import sys


SOURCE_SUFFIXES = [".py"]
BYTECODE_SUFFIXES = []
DEBUG_BYTECODE_SUFFIXES = []
OPTIMIZED_BYTECODE_SUFFIXES = []
if sys.platform == "win32":
    EXTENSION_SUFFIXES = [".pyd"]
else:
    EXTENSION_SUFFIXES = [".so"]


def all_suffixes():
    return SOURCE_SUFFIXES + BYTECODE_SUFFIXES + EXTENSION_SUFFIXES


def _decode_utf8_source(source_bytes):
    """Decode the owned UTF-8 source subset with universal newlines."""
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        source_bytes = source_bytes[3:]
    return (
        source_bytes.decode("utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


class ModuleSpec:
    def __init__(
        self,
        name,
        loader,
        *,
        origin=None,
        loader_state=None,
        is_package=None,
    ):
        if not isinstance(name, str) or name == "":
            raise ValueError("module spec name must be a non-empty string")
        self.name = name
        self.loader = loader
        self.origin = origin
        self.loader_state = loader_state
        self.submodule_search_locations = [] if is_package else None
        self.cached = None
        # A bare ModuleSpec origin can be diagnostic metadata rather than a
        # filesystem location.  ``spec_from_file_location`` opts into the
        # file-backed contract explicitly below, as CPython does.
        self.has_location = False

    @property
    def parent(self):
        if self.submodule_search_locations is not None:
            return self.name
        if "." not in self.name:
            return ""
        return self.name.rsplit(".", 1)[0]

    def __repr__(self):
        return (
            "ModuleSpec(name="
            + repr(self.name)
            + ", loader="
            + repr(self.loader)
            + ")"
        )


class SourceFileLoader:
    """Read-only source loader facade; execution stays ahead-of-time."""

    def __init__(self, fullname, path):
        self.name = fullname
        self.path = str(path)

    def get_filename(self, fullname):
        if fullname != self.name:
            raise ImportError(
                "loader for "
                + repr(self.name)
                + " cannot handle "
                + repr(fullname)
            )
        return self.path

    def get_data(self, path):
        with open(path, "rb") as stream:
            return stream.read()

    def get_source(self, fullname):
        return _decode_utf8_source(self.get_data(self.get_filename(fullname)))

    def is_package(self, fullname):
        filename = os.path.basename(self.get_filename(fullname))
        return filename == "__init__.py"

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise NotImplementedError(
            "SourceFileLoader execution requires runtime Python source compilation; "
            "pcc imports only ahead-of-time linked modules"
        )

    def get_code(self, fullname):
        raise NotImplementedError(
            "pcc does not create runtime Python code objects from source files"
        )

    def load_module(self, fullname=None):
        raise NotImplementedError(
            "legacy loader execution is unavailable in pcc-native mode"
        )


class SourcelessFileLoader:
    def __init__(self, fullname, path):
        self.name = fullname
        self.path = str(path)

    def get_filename(self, fullname):
        if fullname != self.name:
            raise ImportError(
                "loader for "
                + repr(self.name)
                + " cannot handle "
                + repr(fullname)
            )
        return self.path

    def get_code(self, fullname):
        raise NotImplementedError("pcc-native mode does not execute CPython bytecode")

    def exec_module(self, module):
        raise NotImplementedError("pcc-native mode does not execute CPython bytecode")

    def is_package(self, fullname):
        filename = os.path.basename(self.get_filename(fullname))
        return filename.startswith("__init__.") and filename.endswith(".pyc")


class ExtensionFileLoader:
    def __init__(self, fullname, path):
        self.name = fullname
        self.path = str(path)

    def get_filename(self, fullname):
        if fullname != self.name:
            raise ImportError(
                "loader for "
                + repr(self.name)
                + " cannot handle "
                + repr(fullname)
            )
        return self.path

    def is_package(self, fullname):
        filename = os.path.basename(self.get_filename(fullname))
        for suffix in EXTENSION_SUFFIXES:
            if filename == "__init__" + suffix:
                return True
        return False

    def create_module(self, spec):
        raise NotImplementedError(
            "pcc-native extensions are pinned and loaded by the runtime registry, "
            "not instantiated through Python loader objects"
        )

    def exec_module(self, module):
        raise NotImplementedError(
            "pcc-native extensions are executed only by the pinned runtime loader"
        )


class BuiltinImporter:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        raise NotImplementedError(
            "builtin-module finder introspection is not exposed by the linked registry"
        )


class FrozenImporter:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        raise NotImplementedError("pcc-native mode has no frozen Python module format")


class PathFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        raise NotImplementedError(
            "runtime sys.path discovery is unavailable; modules must be linked ahead of time"
        )

    @classmethod
    def invalidate_caches(cls):
        return None


class FileFinder:
    def __init__(self, path, *loader_details):
        self.path = str(path)
        self._loader_details = loader_details

    def find_spec(self, fullname, target=None):
        raise NotImplementedError(
            "FileFinder cannot add modules to an ahead-of-time linked executable"
        )

    def invalidate_caches(self):
        return None


class NamespaceLoader:
    def __init__(self, name, path, path_finder):
        raise NotImplementedError(
            "namespace-package merging is not owned by pcc's linked registry"
        )
