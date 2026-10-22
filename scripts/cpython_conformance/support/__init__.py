"""Minimal ``test.support`` for the CPython ``Lib/test`` conformance harness.

The harness installs this package as ``test.support`` in front of CPython's
``Lib/test`` and runs every test module with it twice -- under CPython for
the baseline and compiled natively by pcc -- so both sides see the same
helpers. The helpers implement the semantics of CPython 3.15's
``Lib/test/support`` with one policy for what a conformance run measures:

- no ``regrtest`` resources are enabled (``requires`` raises
  ``ResourceDenied``, ``requires_resource`` skips);
- nothing spawns an interpreter, forks or opens sockets
  (``has_subprocess_support``, ``has_fork_support`` and
  ``has_socket_support`` are false; ``script_helper`` and ``socket_helper``
  skip);
- CPython implementation details are skipped: ``check_impl_detail``
  answers for an implementation other than CPython (``cpython_only``
  skips) and size/refcount checks skip.

A name a test module imports but this package lacks fails that module's
import under CPython as well; the harness reports such modules separately.
"""

import contextlib
import functools
import os
import re
import sys
import types
import unittest


__all__ = []

# Implementation name the impl-detail guards see under both interpreters.
_IMPLEMENTATION = "conformance"

verbose = 0
use_resources = set()
max_memuse = 0
real_max_memuse = 0
failfast = False
junit_xml_list = None

LOOPBACK_TIMEOUT = 10.0
INTERNET_TIMEOUT = 60.0
SHORT_TIMEOUT = 30.0
LONG_TIMEOUT = 5 * 60.0
SUPPORT_TIMEOUT = SHORT_TIMEOUT

TEST_SUPPORT_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_HOME_DIR = os.path.dirname(TEST_SUPPORT_DIR)
STDLIB_DIR = os.path.dirname(TEST_HOME_DIR)
REPO_ROOT = os.path.dirname(STDLIB_DIR)
TEST_DATA_DIR = os.path.join(TEST_HOME_DIR, "data")

PIPE_MAX_SIZE = 4 * 1024 * 1024 + 1
SOCK_MAX_SIZE = 16 * 1024 * 1024 + 1
MAX_Py_ssize_t = sys.maxsize
_1M = 1024 * 1024
_1G = 1024 * _1M
_2G = 2 * _1G
_4G = 4 * _1G

MS_WINDOWS = sys.platform == "win32"
is_android = False
is_emscripten = False
is_wasi = False
is_wasm32 = False
is_apple_mobile = False
is_apple = sys.platform == "darwin"
is_s390x = False
has_fork_support = False
has_subprocess_support = False
has_socket_support = False
Py_DEBUG = False
Py_GIL_DISABLED = False
Py_TRACE_REFS = False
PGO = False
PGO_EXTENDED = False
TEST_MODULES_ENABLED = False
HAVE_ASAN_FORK_BUG = False
NHASHBITS = 64


class Error(Exception):
    """Base class for regression test exceptions."""


class TestFailed(Error):
    """Test failed."""


class TestDidNotRun(Error):
    """Test did not run any subtests."""


class ResourceDenied(unittest.SkipTest):
    """Test skipped because it requested a disallowed resource."""


def _id(obj):
    return obj


def anticipate_failure(condition):
    if condition:
        return unittest.expectedFailure
    return _id


def get_attribute(obj, name):
    """Get an attribute, raising SkipTest if AttributeError is raised."""
    try:
        attribute = getattr(obj, name)
    except AttributeError:
        raise unittest.SkipTest("object %r has no attribute %r" % (obj, name))
    return attribute


def is_resource_enabled(resource):
    return resource in use_resources


def get_resource_value(resource):
    return None


def requires(resource, msg=None):
    """Raise ResourceDenied: no resource is enabled in a conformance run."""
    if not is_resource_enabled(resource):
        if msg is None:
            msg = "Use of the %r resource not enabled" % resource
        raise ResourceDenied(msg)


def requires_resource(resource):
    if is_resource_enabled(resource):
        return _id
    return unittest.skip(f"resource {resource!r} is not enabled")


def _parse_guards(guards):
    if not guards:
        return ({"cpython": True}, False)
    is_true = list(guards.values())[0]
    return (guards, not is_true)


def check_impl_detail(**guards):
    guards, default = _parse_guards(guards)
    return guards.get(_IMPLEMENTATION, default)


def impl_detail(msg=None, **guards):
    if check_impl_detail(**guards):
        return _id
    if msg is None:
        guardnames, default = _parse_guards(guards)
        names = " or ".join(sorted(guardnames.keys()))
        if default:
            msg = f"implementation detail not available on {names}"
        else:
            msg = f"implementation detail specific to {names}"
    return unittest.skip(msg)


def cpython_only(test):
    """Decorator for tests only applicable on CPython."""
    return impl_detail(cpython=True)(test)


MISSING_C_DOCSTRINGS = False
HAVE_PY_DOCSTRINGS = True
HAVE_DOCSTRINGS = True
requires_docstrings = unittest.skipUnless(HAVE_DOCSTRINGS, "test requires docstrings")


def refcount_test(test):
    return unittest.skip("reference counts are a CPython implementation detail")(test)


def check_sizeof(test, o, size):
    raise unittest.SkipTest("object sizes are a CPython implementation detail")


def calcobjsize(fmt):
    raise unittest.SkipTest("object sizes are a CPython implementation detail")


def calcvobjsize(fmt):
    raise unittest.SkipTest("object sizes are a CPython implementation detail")


def requires_limited_api(test):
    return unittest.skip("needs the limited C API")(test)


def requires_specialization(test):
    return unittest.skip("needs CPython's specializing interpreter")(test)


def requires_gil_enabled(msg="needs the GIL enabled"):
    return unittest.skipIf(Py_GIL_DISABLED, msg)


def expected_failure_if_gil_disabled():
    return _id


def requires_debug_ranges(reason="requires co_positions / debug_ranges"):
    return unittest.skip(reason)


def has_no_debug_ranges():
    return True


def python_is_optimized():
    return True


def no_tracing(func):
    return func


def no_rerun(reason):
    return _id


def thread_unsafe(reason):
    return _id


def skip_if_buildbot(reason=None):
    return _id


def check_sanitizer(*, address=False, memory=False, ub=False, thread=False,
                    function=True):
    return False


def skip_if_sanitizer(reason=None, *, address=False, memory=False, ub=False,
                      thread=False):
    return _id


def skip_if_pgo_task(test):
    return test


def skip_if_unlimited_stack_size(test):
    return test


def skip_if_huge_c_stack(depth=150_000):
    return _id


def skip_emscripten_stack_overflow():
    return unittest.skipIf(is_emscripten, "Exhausts stack on Emscripten")


def skip_wasi_stack_overflow():
    return unittest.skipIf(is_wasi, "Exhausts stack on WASI")


def skip_android_selinux(name):
    return _id


skip_on_s390x = unittest.skipIf(is_s390x, "skipped on s390x")


def requires_fork():
    return unittest.skipUnless(has_fork_support, "requires working os.fork()")


def requires_subprocess():
    return unittest.skipUnless(has_subprocess_support, "requires subprocess support")


def requires_working_socket(*, module=False):
    msg = "requires socket support"
    if module:
        if not has_socket_support:
            raise unittest.SkipTest(msg)
        return None
    return unittest.skipUnless(has_socket_support, msg)


def _requires_unix_version(sysname, min_version):
    return unittest.skip("kernel version checks are not part of a conformance run")


def requires_freebsd_version(*min_version):
    return _requires_unix_version("FreeBSD", min_version)


def requires_linux_version(*min_version):
    return _requires_unix_version("Linux", min_version)


def requires_mac_ver(*min_version):
    return _requires_unix_version("Darwin", min_version)


def _requires_module(name, reason):
    def decorator(test):
        try:
            __import__(name)
        except ImportError:
            return unittest.skip(reason)(test)
        return test
    return decorator


def requires_zlib(reason="requires zlib"):
    return _requires_module("zlib", reason)


def requires_gzip(reason="requires gzip"):
    return _requires_module("gzip", reason)


def requires_bz2(reason="requires bz2"):
    return _requires_module("bz2", reason)


def requires_lzma(reason="requires lzma"):
    return _requires_module("lzma", reason)


def requires_zstd(reason="requires zstd"):
    return unittest.skip(reason)


def requires_IEEE_754(test):
    return test


def system_must_validate_cert(f):
    return f


def findfile(filename, subdir=None):
    """The file in the test directory or on sys.path, else ``filename``."""
    if os.path.isabs(filename):
        return filename
    if subdir is not None:
        filename = os.path.join(subdir, filename)
    for directory in [TEST_HOME_DIR] + sys.path:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            return candidate
    return filename


def sortdict(dict):
    """Like repr(dict), but in sorted order."""
    items = sorted(dict.items())
    return "{%s}" % ", ".join(["%r: %r" % pair for pair in items])


def check_syntax_error(testcase, statement, errtext="", *, lineno=None, offset=None):
    with testcase.assertRaisesRegex(SyntaxError, errtext) as cm:
        compile(statement, "<test string>", "exec")
    err = cm.exception
    testcase.assertIsNotNone(err.lineno)
    if lineno is not None:
        testcase.assertEqual(err.lineno, lineno)
    testcase.assertIsNotNone(err.offset)
    if offset is not None:
        testcase.assertEqual(err.offset, offset)


def open_urlresource(url, *args, **kw):
    raise ResourceDenied("Use of the 'urlfetch' resource not enabled")


@contextlib.contextmanager
def captured_output(stream_name):
    """Temporarily replace the sys stream *stream_name* with a StringIO."""
    import io
    orig = getattr(sys, stream_name)
    setattr(sys, stream_name, io.StringIO())
    try:
        yield getattr(sys, stream_name)
    finally:
        setattr(sys, stream_name, orig)


def captured_stdout():
    return captured_output("stdout")


def captured_stderr():
    return captured_output("stderr")


def captured_stdin():
    return captured_output("stdin")


def gc_collect():
    """Force as many objects as possible to be collected."""
    import gc
    gc.collect()
    gc.collect()
    gc.collect()


@contextlib.contextmanager
def disable_gc():
    import gc
    have_gc = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if have_gc:
            gc.enable()


@contextlib.contextmanager
def gc_threshold(*args):
    import gc
    old_threshold = gc.get_threshold()
    gc.set_threshold(*args)
    try:
        yield
    finally:
        gc.set_threshold(*old_threshold)


def subTests(arg_names, arg_values, /, *, _do_cleanups=False):
    """Run multiple subtests with different parameters."""
    single_param = False
    if isinstance(arg_names, str):
        arg_names = arg_names.replace(",", " ").split()
        if len(arg_names) == 1:
            single_param = True
    arg_values = tuple(arg_values)

    def decorator(func):
        if isinstance(func, type):
            raise TypeError("subTests() can only decorate methods, not classes")

        @functools.wraps(func)
        def wrapper(self, /, *args, **kwargs):
            for values in arg_values:
                if single_param:
                    values = (values,)
                subtest_kwargs = dict(zip(arg_names, values))
                with self.subTest(**subtest_kwargs):
                    func(self, *args, **kwargs, **subtest_kwargs)
                if _do_cleanups:
                    self.doCleanups()
        return wrapper
    return decorator


@contextlib.contextmanager
def run_with_locale(catstr, *locales):
    try:
        import locale
        category = getattr(locale, catstr)
        orig_locale = locale.setlocale(category)
    except AttributeError:
        raise
    except Exception:
        locale = orig_locale = None
        if "" not in locales:
            raise unittest.SkipTest("no locales")
    else:
        for loc in locales:
            try:
                locale.setlocale(category, loc)
                break
            except locale.Error:
                pass
        else:
            if "" not in locales:
                raise unittest.SkipTest(f"no locales {locales}")
    try:
        yield
    finally:
        if locale and orig_locale:
            locale.setlocale(category, orig_locale)


def run_with_locales(catstr, *locales):
    def deco(func):
        @functools.wraps(func)
        def wrapper(self, /, *args, **kwargs):
            dry_run = "" in locales
            try:
                import locale
                category = getattr(locale, catstr)
                orig_locale = locale.setlocale(category)
            except AttributeError:
                raise
            except Exception:
                pass
            else:
                try:
                    for loc in locales:
                        with self.subTest(locale=loc):
                            try:
                                locale.setlocale(category, loc)
                            except locale.Error:
                                self.skipTest(f"no locale {loc!r}")
                            else:
                                dry_run = False
                                func(self, *args, **kwargs)
                finally:
                    locale.setlocale(category, orig_locale)
            if dry_run:
                with self.subTest(locale=None):
                    func(self, *args, **kwargs)
        return wrapper
    return deco


def run_with_tz(tz):
    def decorator(func):
        return unittest.skip("time zone changes are not part of a conformance run")(func)
    return decorator


def bigmemtest(size, memuse, dry_run=True):
    """Bigmem tests run with a small size, or skip when they cannot."""
    def decorator(f):
        @functools.wraps(f)
        def wrapper(self):
            size = wrapper.size
            memuse = wrapper.memuse
            maxsize = 5147
            if not dry_run:
                raise unittest.SkipTest(
                    "not enough memory: %.1fG minimum needed"
                    % (size * memuse / (1024 ** 3)))
            return f(self, maxsize)
        wrapper.size = size
        wrapper.memuse = memuse
        return wrapper
    return decorator


def bigaddrspacetest(f):
    return unittest.skip("large address space tests are not part of a conformance run")(f)


def nomemtest(f):
    return f


def flush_std_streams():
    if sys.stdout is not None:
        sys.stdout.flush()
    if sys.stderr is not None:
        sys.stderr.flush()


def print_warning(msg):
    flush_std_streams()
    for line in (msg or " ").splitlines():
        print(f"Warning -- {line}", file=sys.__stderr__, flush=True)


def reap_children():
    """No child processes are started in a conformance run."""


@contextlib.contextmanager
def swap_attr(obj, attr, new_val):
    """Temporarily swap out an attribute with a new object."""
    if hasattr(obj, attr):
        real_val = getattr(obj, attr)
        setattr(obj, attr, new_val)
        try:
            yield real_val
        finally:
            setattr(obj, attr, real_val)
    else:
        setattr(obj, attr, new_val)
        try:
            yield
        finally:
            if hasattr(obj, attr):
                delattr(obj, attr)


@contextlib.contextmanager
def swap_item(obj, item, new_val):
    """Temporarily swap out an item with a new object."""
    if item in obj:
        real_val = obj[item]
        obj[item] = new_val
        try:
            yield real_val
        finally:
            obj[item] = real_val
    else:
        obj[item] = new_val
        try:
            yield
        finally:
            if item in obj:
                del obj[item]


def args_from_interpreter_flags():
    return []


def optim_args_from_interpreter_flags():
    return []


class Matcher(object):
    _partial_matches = ("msg", "message")

    def matches(self, d, **kwargs):
        result = True
        for k in kwargs:
            v = kwargs[k]
            dv = d.get(k)
            if not self.match_value(k, dv, v):
                result = False
                break
        return result

    def match_value(self, k, dv, v):
        if type(v) != type(dv):
            result = False
        elif type(dv) is not str or k not in self._partial_matches:
            result = v == dv
        else:
            result = dv.find(v) >= 0
        return result


def detect_api_mismatch(ref_api, other_api, *, ignore=()):
    missing_items = set(dir(ref_api)) - set(dir(other_api))
    if ignore:
        missing_items -= set(ignore)
    missing_items = set(m for m in missing_items
                        if not m.startswith("_") or m.endswith("__"))
    return missing_items


def check__all__(test_case, module, name_of_module=None, extra=(),
                 not_exported=()):
    """Assert that ``module.__all__`` lists exactly its public names."""
    if name_of_module is None:
        name_of_module = (module.__name__,)
    elif isinstance(name_of_module, str):
        name_of_module = (name_of_module,)
    expected = set(extra)
    for name in dir(module):
        if name.startswith("_") or name in not_exported:
            continue
        obj = getattr(module, name)
        if (getattr(obj, "__module__", None) in name_of_module or
                (not hasattr(obj, "__module__") and
                 not isinstance(obj, types.ModuleType))):
            expected.add(name)
    test_case.assertCountEqual(module.__all__, expected)


class SuppressCrashReport:
    """No crash reporter is involved in a conformance run."""

    def __enter__(self):
        return self

    def __exit__(self, *ignore_exc):
        return None


def patch(test_instance, object_to_patch, attr_name, new_value):
    """Override 'object_to_patch'.'attr_name' with 'new_value' until cleanup."""
    attr_is_local = False
    try:
        old_value = object_to_patch.__dict__[attr_name]
    except (AttributeError, KeyError):
        old_value = getattr(object_to_patch, attr_name, None)
    else:
        attr_is_local = True

    def cleanup():
        if attr_is_local:
            setattr(object_to_patch, attr_name, old_value)
        else:
            delattr(object_to_patch, attr_name)

    test_instance.addCleanup(cleanup)
    setattr(object_to_patch, attr_name, new_value)


def run_in_subinterp(code):
    raise unittest.SkipTest("subinterpreters are not part of a conformance run")


def run_in_subinterp_with_config(code, *, own_gil=None, **config):
    raise unittest.SkipTest("subinterpreters are not part of a conformance run")


def check_free_after_iterating(test, iter, cls, args=()):
    done = False

    def wrapper():
        class A(cls):
            def __del__(self):
                nonlocal done
                try:
                    next(it)
                except StopIteration:
                    done = True
        return A
    it = iter(wrapper()(*args))
    # Issue 26494: Shouldn't crash
    test.assertRaises(StopIteration, next, it)
    gc_collect()
    test.assertTrue(done)


def missing_compiler_executable(cmd_names=[]):
    return "cc"


def setswitchinterval(interval):
    return sys.setswitchinterval(interval)


def get_pagesize():
    return 4096


@contextlib.contextmanager
def disable_faulthandler():
    yield


class SaveSignals:
    def save(self):
        pass

    def restore(self):
        pass


def with_pymalloc():
    return False


def with_mimalloc():
    return False


class _ALWAYS_EQ:
    """Object that is equal to anything."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False


ALWAYS_EQ = _ALWAYS_EQ()


class _NEVER_EQ:
    """Object that is not equal to anything."""

    def __eq__(self, other):
        return False

    def __ne__(self, other):
        return True

    def __hash__(self):
        return 1


NEVER_EQ = _NEVER_EQ()


class _LARGEST:
    """Object that is greater than anything (except itself).

    The comparisons functools.total_ordering derives from __lt__ and __eq__
    are spelled out.
    """

    def __eq__(self, other):
        return isinstance(other, _LARGEST)

    def __lt__(self, other):
        return False

    def __gt__(self, other):
        return not isinstance(other, _LARGEST)

    def __le__(self, other):
        return isinstance(other, _LARGEST)

    def __ge__(self, other):
        return True


LARGEST = _LARGEST()


class _SMALLEST:
    """Object that is less than anything (except itself).

    The comparisons functools.total_ordering derives from __gt__ and __eq__
    are spelled out.
    """

    def __eq__(self, other):
        return isinstance(other, _SMALLEST)

    def __gt__(self, other):
        return False

    def __lt__(self, other):
        return not isinstance(other, _SMALLEST)

    def __ge__(self, other):
        return isinstance(other, _SMALLEST)

    def __le__(self, other):
        return True


SMALLEST = _SMALLEST()


def maybe_get_event_loop_policy():
    import asyncio.events
    return asyncio.events._get_event_loop_policy()


class catch_unraisable_exception:
    """Catch unraisable exceptions through sys.unraisablehook."""

    def __init__(self):
        self.unraisable = None
        self._old_hook = None

    def _hook(self, unraisable):
        self.unraisable = unraisable

    def __enter__(self):
        self._old_hook = sys.unraisablehook
        sys.unraisablehook = self._hook
        return self

    def __exit__(self, *exc_info):
        sys.unraisablehook = self._old_hook
        del self.unraisable


def wait_process(pid, *, exitcode, timeout=None):
    raise unittest.SkipTest("no child processes in a conformance run")


def check_disallow_instantiation(testcase, tp, *args, **kwds):
    """Check that the given type cannot be instantiated."""
    mod = tp.__module__
    name = tp.__name__
    if mod != "builtins":
        qualname = f"{mod}.{name}"
    else:
        qualname = f"{name}"
    msg = f"cannot create '{re.escape(qualname)}' instances"
    testcase.assertRaisesRegex(TypeError, msg, tp, *args, **kwds)
    testcase.assertRaisesRegex(TypeError, msg, tp.__new__, tp, *args, **kwds)


def get_recursion_depth():
    """The caller's recursion depth, counted through its frames."""
    depth = 0
    frame = sys._getframe()
    while frame is not None:
        depth += 1
        frame = frame.f_back
    frame = None
    return max(depth - 1, 1)


def get_recursion_available():
    limit = sys.getrecursionlimit()
    depth = get_recursion_depth()
    return limit - depth


@contextlib.contextmanager
def set_recursion_limit(limit):
    """Temporarily change the recursion limit."""
    original_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(limit)
        yield
    finally:
        sys.setrecursionlimit(original_limit)


def infinite_recursion(max_depth=None):
    if max_depth is None:
        max_depth = 20_000
    elif max_depth < 3:
        raise ValueError(f"max_depth must be at least 3, got {max_depth}")
    depth = get_recursion_depth()
    depth = max(depth - 1, 1)
    limit = depth + max_depth
    return set_recursion_limit(limit)


def exceeds_recursion_limit():
    """For recursion tests, easily exceeds default recursion limit."""
    return 150_000


@contextlib.contextmanager
def adjust_int_max_str_digits(max_digits):
    """Temporarily change the integer string conversion length limit."""
    current = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(max_digits)
        yield
    finally:
        sys.set_int_max_str_digits(current)


def ignore_deprecations_from(module, *, like):
    import warnings
    token = object()
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        module=module,
        message=like + fr"(?#support{id(token)})",
    )
    return token


def clear_ignored_deprecations(*tokens):
    import warnings
    if not tokens:
        raise ValueError("Provide token or tokens returned by ignore_deprecations_from")
    new_filters = []
    old_filters = warnings._get_filters()
    endswith = tuple(rf"(?#support{id(token)})" for token in tokens)
    for action, message, category, module, lineno in old_filters:
        if action == "ignore" and category is DeprecationWarning:
            if isinstance(message, re.Pattern):
                msg = message.pattern
            else:
                msg = message or ""
            if msg.endswith(endswith):
                continue
        new_filters.append((action, message, category, module, lineno))
    if old_filters != new_filters:
        old_filters[:] = new_filters
        warnings._filters_mutated()


def requires_venv_with_pip():
    return unittest.skip("venv/pip are not part of a conformance run")


def late_deletion(obj):
    return None


def busy_retry(timeout, err_msg=None, /, *, error=True):
    import time
    start_time = time.monotonic()
    deadline = start_time + timeout
    while True:
        yield
        if time.monotonic() >= deadline:
            break
    if error:
        msg = f"timeout ({timeout:.1f} seconds)"
        if err_msg:
            msg = f"{msg}: {err_msg}"
        raise AssertionError(msg)


def sleeping_retry(timeout, err_msg=None, /, *, init_delay=0.010, max_delay=1.0,
                   error=True):
    import time
    delay = init_delay
    for _ in busy_retry(timeout, err_msg, error=error):
        yield
        time.sleep(delay)
        delay = min(delay * 2, max_delay)


class Stopwatch:
    """Context manager measuring elapsed time with time.perf_counter."""

    def __enter__(self):
        import time
        self.clock = time.perf_counter
        self.start = self.clock()
        return self

    def __exit__(self, *exc):
        self.stop = self.clock()
        self.seconds = self.stop - self.start


def walk_class_hierarchy(top, *, topdown=True):
    stack = [top]
    while stack:
        top = stack.pop()
        if isinstance(top, tuple):
            yield top
            continue
        subs = type(top).__subclasses__(top)
        if topdown:
            yield top, subs
            for sub in reversed(subs):
                stack.append(sub)
        else:
            stack.append((top, subs))
            for sub in reversed(subs):
                stack.append(sub)


def force_color(color):
    return contextlib.nullcontext()


def force_colorized(func):
    return func


def force_not_colorized(func):
    return func


def force_colorized_test_class(cls):
    return cls


def force_not_colorized_test_class(cls):
    return cls


def make_clean_env():
    clean_env = os.environ.copy()
    for k in list(clean_env):
        if k.startswith("PYTHON"):
            clean_env.pop(k)
    clean_env.pop("FORCE_COLOR", None)
    clean_env.pop("NO_COLOR", None)
    return clean_env


class BrokenIter:
    def __init__(self, init_raises=False, next_raises=False, iter_raises=False):
        if init_raises:
            1 / 0
        self.next_raises = next_raises
        self.iter_raises = iter_raises

    def __next__(self):
        if self.next_raises:
            1 / 0

    def __iter__(self):
        if self.iter_raises:
            1 / 0
        return self


def in_systemd_nspawn_sync_suppressed():
    return False


def run_no_yield_async_fn(async_fn, /, *args, **kwargs):
    coro = async_fn(*args, **kwargs)
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    else:
        raise AssertionError("coroutine did not complete")
    finally:
        coro.close()


def run_yielding_async_fn(async_fn, /, *args, **kwargs):
    coro = async_fn(*args, **kwargs)
    try:
        while True:
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
    finally:
        coro.close()


def is_libssl_fips_mode():
    return False


def support_remote_exec_only(test):
    return unittest.skip("remote execution is not part of a conformance run")(test)


def requires_remote_subprocess_debugging():
    return unittest.skip("remote debugging is not part of a conformance run")


def linked_to_musl():
    return False


def control_characters_c0():
    return [chr(c) for c in range(0x00, 0x20)] + ["\x7F"]


def get_signal_name(exitcode):
    return None


def load_package_tests(pkg_dir, loader, standard_tests, pattern):
    """Generic load_tests implementation for simple test packages."""
    if pattern is None:
        pattern = "test*"
    package_tests = loader.discover(start_dir=pkg_dir, top_level_dir=STDLIB_DIR,
                                    pattern=pattern)
    standard_tests.addTests(package_tests)
    return standard_tests


def run_code(code, extra_names=None):
    """Run a piece of code after dedenting it, and return its global namespace."""
    import textwrap
    ns = {}
    if extra_names:
        ns.update(extra_names)
    exec(textwrap.dedent(code), ns)
    return ns


class EqualToForwardRef:
    """Helper to ease use of annotationlib.ForwardRef in tests."""

    def __init__(self, arg, *, module=None, owner=None, is_class=False):
        self.__forward_arg__ = arg
        self.__forward_is_class__ = is_class
        self.__forward_module__ = module
        self.__owner__ = owner

    def __eq__(self, other):
        import annotationlib
        if not isinstance(other, (EqualToForwardRef, annotationlib.ForwardRef)):
            return NotImplemented
        return (
            self.__forward_arg__ == other.__forward_arg__
            and self.__forward_module__ == other.__forward_module__
            and self.__forward_is_class__ == other.__forward_is_class__
            and self.__owner__ == other.__owner__
        )

    def __repr__(self):
        extra = []
        if self.__forward_module__ is not None:
            extra.append(f", module={self.__forward_module__!r}")
        if self.__forward_is_class__:
            extra.append(", is_class=True")
        if self.__owner__ is not None:
            extra.append(f", owner={self.__owner__!r}")
        return f"EqualToForwardRef({self.__forward_arg__!r}{''.join(extra)})"


def skip_if_broken_multiprocessing_synchronize():
    raise unittest.SkipTest("multiprocessing needs subprocesses")


def skip_if_buggy_ucrt_strfptime(test):
    return test


@contextlib.contextmanager
def patch_list(orig):
    """Like unittest.mock.patch.dict, but for lists."""
    try:
        saved = orig[:]
        yield
    finally:
        orig[:] = saved


def darwin_malloc_err_warning(test_name):
    """No notice is printed in a conformance run."""


def reset_code(f):
    """Specializations and JIT code are CPython implementation details."""
    return f


_JIT_ENABLED = False
requires_jit_enabled = unittest.skipUnless(_JIT_ENABLED, "requires JIT enabled")
requires_jit_disabled = unittest.skipIf(_JIT_ENABLED, "requires JIT disabled")

_ROOT_IN_POSIX = hasattr(os, "geteuid") and os.geteuid() == 0
requires_root_user = unittest.skipUnless(_ROOT_IN_POSIX, "test needs root privilege")
requires_non_root_user = unittest.skipIf(_ROOT_IN_POSIX, "test needs non-root account")


class PythonSymlink:
    """Creating a symlinked interpreter is not part of a conformance run."""

    def __init__(self, link=None):
        raise unittest.SkipTest("needs a separate interpreter")


def async_yield(v):
    """Yield ``v`` from a coroutine, as CPython's ``types.coroutine`` helper."""
    return types.coroutine(_async_yield_generator)(v)


def _async_yield_generator(v):
    return (yield v)


def _strftime_extensions():
    """Whether strftime() supports glibc extensions like '%4Y'."""
    import time
    try:
        return time.strftime("%4Y") != "%4Y"
    except ValueError:
        return False


has_strftime_extensions = sys.platform != "win32" and _strftime_extensions()


_BASE_COPY_SRC_DIR_IGNORED_NAMES = frozenset({".git", "__pycache__"})


def copy_python_src_ignore(path, names):
    """Ignore function for shutil.copytree() copying the Python source."""
    ignored = _BASE_COPY_SRC_DIR_IGNORED_NAMES
    if os.path.basename(path) == "Doc":
        ignored |= {"build", "venv"}
    elif "Modules" in names:
        ignored |= {"build"}
    return ignored


unix_shell = "/system/bin/sh" if is_android else "/bin/sh"
