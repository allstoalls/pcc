"""``test.support.warnings_helper`` for the conformance harness."""

import contextlib
import importlib
import re
import sys
import warnings


def import_deprecated(name):
    """Import *name* while suppressing DeprecationWarning."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=DeprecationWarning)
        return importlib.import_module(name)


def check_syntax_warning(testcase, statement, errtext="", *, lineno=1, offset=None):
    from test.support import check_syntax_error
    with warnings.catch_warnings(record=True) as warns:
        warnings.simplefilter("always", SyntaxWarning)
        compile(statement, "<testcase>", "exec")
    testcase.assertEqual(len(warns), 1, warns)
    warn, = warns
    testcase.assertIsSubclass(warn.category, SyntaxWarning)
    if errtext:
        testcase.assertRegex(str(warn.message), errtext)
    testcase.assertEqual(warn.filename, "<testcase>")
    testcase.assertIsNotNone(warn.lineno)
    if lineno is not None:
        testcase.assertEqual(warn.lineno, lineno)
    with warnings.catch_warnings(record=True) as warns:
        warnings.simplefilter("error", SyntaxWarning)
        check_syntax_error(testcase, statement, errtext, lineno=lineno, offset=offset)
    testcase.assertEqual(warns, [])


@contextlib.contextmanager
def ignore_warnings(*, category, message=""):
    """Suppress warnings of ``category``; a decorator or a context manager."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=category, message=message)
        yield


@contextlib.contextmanager
def ignore_fork_in_thread_deprecation_warnings():
    with ignore_warnings(message=".*fork.*may lead to deadlocks in the child.*",
                         category=DeprecationWarning):
        yield


class WarningsRecorder(object):
    """The warnings list of ``catch_warnings(record=True)``."""

    def __init__(self, warnings_list):
        self._warnings = warnings_list
        self._last = 0

    def __getattr__(self, attr):
        if len(self._warnings) > self._last:
            return getattr(self._warnings[-1], attr)
        elif attr in warnings.WarningMessage._WARNING_DETAILS:
            return None
        raise AttributeError("%r has no attribute %r" % (self, attr))

    @property
    def warnings(self):
        return self._warnings[self._last:]

    def reset(self):
        self._last = len(self._warnings)


@contextlib.contextmanager
def check_warnings(*filters, **kwargs):
    """Silence warnings and check that every given filter caught one."""
    quiet = kwargs.get("quiet")
    if not filters:
        filters = (("", Warning),)
        if quiet is None:
            quiet = True
    return _filterwarnings(filters, quiet)


@contextlib.contextmanager
def check_no_warnings(testcase, message="", category=Warning, force_gc=False):
    """Check that no warnings are emitted with ``category`` enabled."""
    from test.support import gc_collect
    with warnings.catch_warnings(record=True) as warns:
        warnings.filterwarnings("always", message=message, category=category)
        yield
        if force_gc:
            gc_collect()
    testcase.assertEqual(warns, [])


@contextlib.contextmanager
def check_no_resource_warning(testcase):
    with check_no_warnings(testcase, category=ResourceWarning, force_gc=True):
        yield


def _filterwarnings(filters, quiet=False):
    """Catch the warnings, check the expected ones, re-raise the others."""
    try:
        frame = sys._getframe(2)
    except (AttributeError, ValueError):
        frame = None
    if frame is not None:
        registry = frame.f_globals.get("__warningregistry__")
        if registry:
            registry.clear()
    wmod = sys.modules["warnings"]
    with wmod.catch_warnings(record=True) as w:
        wmod.simplefilter("always")
        yield WarningsRecorder(w)
    reraise = list(w)
    missing = []
    for msg, cat in filters:
        seen = False
        for w in reraise[:]:
            warning = w.message
            if (re.match(msg, str(warning), re.I) and
                    issubclass(warning.__class__, cat)):
                seen = True
                reraise.remove(w)
        if not seen and not quiet:
            missing.append((msg, cat.__name__))
    if reraise:
        raise AssertionError("unhandled warning %s" % reraise[0])
    if missing:
        raise AssertionError("filter (%r, %s) did not catch any warning" % missing[0])


@contextlib.contextmanager
def save_restore_warnings_filters():
    old_filters = warnings.filters[:]
    try:
        yield
    finally:
        warnings.filters[:] = old_filters


def _warn_about_deprecation():
    warnings.warn(
        "This is used in test_support test to ensure"
        " support.ignore_deprecations_from() works as expected."
        " You should not be seeing this.",
        DeprecationWarning,
        stacklevel=0,
    )
