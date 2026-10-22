"""``test.support.threading_helper`` for the conformance harness.

Thread accounting uses ``threading.active_count()`` where CPython's helper
reads ``_thread._count()`` and ``threading._dangling``; both only decide
whether to warn or wait.
"""

import contextlib
import functools
import sys
import threading
import time
import unittest

from test import support


def threading_setup():
    return threading.active_count(), 0


def threading_cleanup(*original_values):
    orig_count = original_values[0]
    for _ in support.sleeping_retry(1.0, error=False):
        if threading.active_count() <= orig_count:
            return


def reap_threads(func):
    """Wait for the threads a test started, even when it fails."""
    @functools.wraps(func)
    def decorator(*args):
        key = threading_setup()
        try:
            return func(*args)
        finally:
            threading_cleanup(*key)
    return decorator


@contextlib.contextmanager
def wait_threads_exit(timeout=None):
    """Wait until the threads created in the with block exit."""
    if timeout is None:
        timeout = support.SHORT_TIMEOUT
    old_count = threading.active_count()
    try:
        yield
    finally:
        start_time = time.monotonic()
        for _ in support.sleeping_retry(timeout, error=False):
            support.gc_collect()
            count = threading.active_count()
            if count <= old_count:
                break
        else:
            dt = time.monotonic() - start_time
            raise AssertionError(
                f"wait_threads() failed to cleanup {count - old_count} "
                f"threads after {dt:.1f} seconds "
                f"(count: {count}, old count: {old_count})")


def join_thread(thread, timeout=None):
    """Join a thread; AssertionError if it is still alive after timeout."""
    if timeout is None:
        timeout = support.SHORT_TIMEOUT
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"failed to join the thread in {timeout:.1f} seconds")


@contextlib.contextmanager
def start_threads(threads, unlock=None):
    threads = list(threads)
    started = []
    try:
        for t in threads:
            t.start()
            started.append(t)
        yield
    finally:
        try:
            if unlock:
                unlock()
            endtime = time.monotonic()
            for timeout in range(1, 16):
                endtime += 60
                for t in started:
                    t.join(max(endtime - time.monotonic(), 0.01))
                started = [t for t in started if t.is_alive()]
                if not started:
                    break
        finally:
            started = [t for t in started if t.is_alive()]
            if started:
                raise AssertionError("Unable to join %d threads" % len(started))


class catch_threading_exception:
    """Catch a threading.Thread exception through threading.excepthook."""

    def __init__(self):
        self.exc_type = None
        self.exc_value = None
        self.exc_traceback = None
        self.thread = None
        self._old_hook = None

    def _hook(self, args):
        self.exc_type = args.exc_type
        self.exc_value = args.exc_value
        self.exc_traceback = args.exc_traceback
        self.thread = args.thread

    def __enter__(self):
        self._old_hook = threading.excepthook
        threading.excepthook = self._hook
        return self

    def __exit__(self, *exc_info):
        threading.excepthook = self._old_hook
        del self.exc_type
        del self.exc_value
        del self.exc_traceback
        del self.thread


can_start_thread = sys.platform not in ("emscripten", "wasi")


def requires_working_threading(*, module=False):
    msg = "requires threading support"
    if module:
        if not can_start_thread:
            raise unittest.SkipTest(msg)
        return None
    return unittest.skipUnless(can_start_thread, msg)


def run_concurrently(worker_func, nthreads=None, args=(), kwargs={}):
    """Run the worker function(s) concurrently in multiple threads."""
    from collections.abc import Iterable
    if nthreads is None:
        nthreads = len(worker_func)
    if not isinstance(worker_func, Iterable):
        worker_func = [worker_func] * nthreads
    barrier = threading.Barrier(nthreads)

    def wrapper_func(func, *args, **kwargs):
        barrier.wait()
        func(*args, **kwargs)

    with catch_threading_exception() as cm:
        workers = [threading.Thread(target=wrapper_func, args=(func, *args), kwargs=kwargs)
                   for func in worker_func]
        with start_threads(workers):
            pass
        if cm.exc_value is not None:
            raise cm.exc_value
