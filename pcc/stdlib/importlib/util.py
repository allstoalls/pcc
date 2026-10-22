"""Pure metadata helpers for pcc's finite :mod:`importlib` provider."""
from __future__ import annotations

import os
import sys

from . import _resolve_name
from .machinery import ModuleSpec, SourceFileLoader, _decode_utf8_source


def resolve_name(name, package):
    return _resolve_name(name, package)


def _cache_tag():
    implementation = getattr(sys, "implementation", None)
    tag = getattr(implementation, "cache_tag", None)
    if not isinstance(tag, str) or tag == "":
        raise NotImplementedError("this runtime does not define a bytecode cache tag")
    return tag


def cache_from_source(path, debug_override=None, *, optimization=None):
    """Return the conventional cache filename without claiming a bytecode loader."""
    if debug_override is not None:
        if optimization is not None:
            raise TypeError("debug_override or optimization must be set, not both")
        optimization = "" if debug_override else "1"
    head, tail = os.path.split(str(path))
    if not tail.endswith(".py"):
        raise NotImplementedError(
            "cache metadata currently owns only .py source paths"
        )
    stem = tail[:-3]
    filename = stem + "." + _cache_tag()
    if optimization is not None and str(optimization) != "":
        optimization = str(optimization)
        if not optimization.isalnum():
            raise ValueError("optimization must be an alphanumeric string")
        filename += ".opt-" + optimization
    filename += ".pyc"
    return os.path.join(head, "__pycache__", filename)


def source_from_cache(path):
    path = str(path)
    cache_dir, filename = os.path.split(path)
    head, cache_name = os.path.split(cache_dir)
    if cache_name != "__pycache__":
        raise ValueError("__pycache__ not bottom-level directory in " + repr(path))
    tag_marker = "." + _cache_tag()
    marker_index = filename.rfind(tag_marker)
    if marker_index <= 0 or not filename.endswith(".pyc"):
        raise ValueError("expected a cache filename with the runtime cache tag")
    suffix = filename[marker_index + len(tag_marker) : -4]
    if suffix != "":
        if not suffix.startswith(".opt-"):
            raise ValueError("invalid bytecode cache filename")
        optimization = suffix[len(".opt-") :]
        if optimization == "" or not optimization.isalnum():
            raise ValueError("invalid bytecode cache optimization tag")
    return os.path.join(head, filename[:marker_index] + ".py")


def spec_from_loader(name, loader, *, origin=None, is_package=None):
    if is_package is None and hasattr(loader, "is_package"):
        is_package = loader.is_package(name)
    return ModuleSpec(
        name,
        loader,
        origin=origin,
        is_package=is_package,
    )


def spec_from_file_location(
    name,
    location,
    *,
    loader=None,
    submodule_search_locations=None,
):
    location = os.path.abspath(str(location))
    if loader is None:
        if not location.endswith(".py"):
            raise NotImplementedError(
                "automatic loader selection owns only UTF-8 Python source metadata"
            )
        loader = SourceFileLoader(name, location)
    is_package = submodule_search_locations is not None
    if submodule_search_locations is None and hasattr(loader, "is_package"):
        is_package = loader.is_package(name)
    spec = ModuleSpec(name, loader, origin=location, is_package=is_package)
    spec.has_location = True
    if submodule_search_locations is not None:
        spec.submodule_search_locations = list(submodule_search_locations)
    elif is_package:
        spec.submodule_search_locations = [os.path.dirname(location)]
    return spec


def decode_source(source_bytes):
    """Decode UTF-8 source; other coding-cookie encodings are not owned."""
    return _decode_utf8_source(source_bytes)


def find_spec(name, package=None):
    raise NotImplementedError(
        "runtime module discovery is unavailable; pcc modules are selected "
        "and linked at compile time"
    )


def module_from_spec(spec):
    raise NotImplementedError(
        "module_from_spec requires runtime module construction and execution"
    )


class LazyLoader:
    def __init__(self, loader):
        raise NotImplementedError(
            "lazy runtime module execution is unavailable for linked pcc modules"
        )
