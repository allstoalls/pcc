"""``test.support.import_helper`` for the conformance harness.

CPython's internal test extension modules (``_testcapi``,
``_testinternalcapi`` and the other ``_test*`` modules) are implementation
details: importing one skips under both interpreters. The frozen-module
override of CPython's ``_imp`` is not part of a conformance run.
"""

import contextlib
import importlib
import os
import sys
import unittest
import warnings

from .os_helper import temp_dir, unlink


def _cpython_test_extension(name):
    return name.split(".")[0].startswith("_test")


@contextlib.contextmanager
def _ignore_deprecated_imports(ignore=True):
    if ignore:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", ".+ (module|package)", DeprecationWarning)
            yield
    else:
        yield


def unload(name):
    try:
        del sys.modules[name]
    except KeyError:
        pass


def forget(modname):
    """Remove the module from sys.modules and delete its legacy .pyc files."""
    unload(modname)
    for dirname in sys.path:
        source = os.path.join(dirname, modname + ".py")
        unlink(source + "c")


def import_module(name, deprecated=False, *, required_on=()):
    """Import and return the module, raising SkipTest if it is unavailable."""
    if _cpython_test_extension(name):
        raise unittest.SkipTest("No module named %r (CPython test extension)" % name)
    with _ignore_deprecated_imports(deprecated):
        try:
            return importlib.import_module(name)
        except ImportError as msg:
            if sys.platform.startswith(tuple(required_on)):
                raise
            raise unittest.SkipTest(str(msg))


def _save_and_remove_modules(names):
    orig_modules = {}
    prefixes = tuple(name + "." for name in names)
    for modname in list(sys.modules):
        if modname in names or modname.startswith(prefixes):
            orig_modules[modname] = sys.modules.pop(modname)
    return orig_modules


@contextlib.contextmanager
def frozen_modules(enabled=True):
    yield


def import_fresh_module(name, fresh=(), blocked=(), *, deprecated=False,
                        usefrozen=False):
    """Import a fresh copy of ``name``, bypassing sys.modules."""
    with _ignore_deprecated_imports(deprecated):
        fresh = list(fresh)
        blocked = list(blocked)
        names = {name, *fresh, *blocked}
        orig_modules = _save_and_remove_modules(names)
        for modname in blocked:
            sys.modules[modname] = None
        try:
            try:
                for modname in fresh:
                    __import__(modname)
            except ImportError:
                return None
            return importlib.import_module(name)
        finally:
            _save_and_remove_modules(names)
            sys.modules.update(orig_modules)


class CleanImport(object):
    """Force import to return a new module reference."""

    def __init__(self, *module_names, usefrozen=False):
        self.original_modules = sys.modules.copy()
        for module_name in module_names:
            if module_name in sys.modules:
                module = sys.modules[module_name]
                if module.__name__ != module_name:
                    del sys.modules[module.__name__]
                del sys.modules[module_name]

    def __enter__(self):
        return self

    def __exit__(self, *ignore_exc):
        sys.modules.update(self.original_modules)


class DirsOnSysPath(object):
    """Temporarily add directories to sys.path."""

    def __init__(self, *paths):
        self.original_value = sys.path[:]
        self.original_object = sys.path
        sys.path.extend(paths)

    def __enter__(self):
        return self

    def __exit__(self, *ignore_exc):
        sys.path = self.original_object
        sys.path[:] = self.original_value


def modules_setup():
    return sys.modules.copy(),


def modules_cleanup(oldmodules):
    encodings = [(k, v) for k, v in sys.modules.items() if k.startswith("encodings.")]
    sys.modules.clear()
    sys.modules.update(encodings)
    sys.modules.update(oldmodules)


@contextlib.contextmanager
def isolated_modules():
    (saved,) = modules_setup()
    try:
        yield
    finally:
        modules_cleanup(saved)


@contextlib.contextmanager
def ready_to_import(name=None, source=""):
    from test.support import script_helper
    name = name or "spam"
    with temp_dir() as tempdir:
        path = script_helper.make_script(tempdir, name, source)
        old_module = sys.modules.pop(name, None)
        try:
            sys.path.insert(0, tempdir)
            yield name, path
        finally:
            sys.path.remove(tempdir)
            if old_module is not None:
                sys.modules[name] = old_module
            else:
                sys.modules.pop(name, None)


def ensure_lazy_imports(imported_module, modules_to_block, *, additional_code=None):
    raise unittest.SkipTest("needs a separate interpreter process")


@contextlib.contextmanager
def module_restored(name):
    """Restore a module to its original state on exit."""
    missing = object()
    orig = sys.modules.get(name, missing)
    if orig is None:
        mod = importlib.import_module(name)
    else:
        mod = type(sys)(name)
        mod.__dict__.update(orig.__dict__)
        sys.modules[name] = mod
    try:
        yield mod
    finally:
        if orig is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig


def create_module(name, loader=None, *, ispkg=False):
    """Return a new, empty module."""
    import importlib.machinery
    import importlib.util
    spec = importlib.machinery.ModuleSpec(name, loader, origin="<import_helper>",
                                          is_package=ispkg)
    return importlib.util.module_from_spec(spec)


def make_legacy_pyc(source, allow_compile=False):
    raise unittest.SkipTest("bytecode files are a CPython implementation detail")


def mock_register_at_fork(func):
    from unittest import mock
    return mock.patch("os.register_at_fork", create=True)(func)
