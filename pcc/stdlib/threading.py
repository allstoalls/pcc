"""Minimal native-backed threading shim for pcc.

Every native runtime is threaded: Thread.start runs the target on a new
native thread that executes in parallel with the caller (there is no GIL).
"""
import functools

from pcc.extern import extern, c_int64, c_ptr, c_obj

_get_ident = extern("py_threading_get_ident", (), c_int64)
_lock_new = extern("py_threading_lock_new", (), c_obj)
_lock_acquire = extern("py_threading_lock_acquire", (c_ptr,), c_int64)
_lock_release = extern("py_threading_lock_release", (c_ptr,), c_int64)
_rlock_new = extern("py_threading_rlock_new", (), c_obj)
_rlock_acquire = extern("py_threading_rlock_acquire", (c_ptr,), c_int64)
_rlock_release = extern("py_threading_rlock_release", (c_ptr,), c_int64)
_event_new = extern("py_threading_event_new", (), c_obj)
_event_set = extern("py_threading_event_set", (c_ptr,), c_int64)
_event_clear = extern("py_threading_event_clear", (c_ptr,), c_int64)
_event_is_set = extern("py_threading_event_is_set", (c_ptr,), c_int64)
_event_wait = extern("py_threading_event_wait", (c_ptr,), c_int64)
_thread_new = extern("py_threading_thread_new", (c_ptr, c_ptr), c_obj)
_thread_start = extern("py_threading_thread_start", (c_ptr,), c_int64)
_thread_join = extern("py_threading_thread_join", (c_ptr,), c_int64)
_thread_is_alive = extern("py_threading_thread_is_alive", (c_ptr,), c_int64)
_condition_new = extern("py_threading_condition_new", (c_ptr,), c_obj)
_condition_acquire = extern("py_threading_condition_acquire", (c_ptr,), c_int64)
_condition_release = extern("py_threading_condition_release", (c_ptr,), c_int64)
_condition_wait = extern("py_threading_condition_wait", (c_ptr,), c_int64)
_condition_wait_timeout = extern("py_threading_condition_wait_timeout", (c_ptr, c_ptr), c_int64)
_condition_notify = extern("py_threading_condition_notify", (c_ptr,), c_int64)
_condition_notify_all = extern("py_threading_condition_notify_all", (c_ptr,), c_int64)
_sem_new = extern("py_threading_semaphore_new", (c_int64,), c_obj)
_sem_acquire = extern("py_threading_semaphore_acquire", (c_ptr,), c_int64)
_sem_release = extern("py_threading_semaphore_release", (c_ptr,), c_int64)


def get_ident() -> int:
    return _get_ident()


def current_thread():
    return get_ident()


class Lock:
    def __init__(self) -> None:
        self._ptr = _lock_new()

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        return _lock_acquire(self._ptr) == 0

    def release(self) -> None:
        _lock_release(self._ptr)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return None


class RLock:
    def __init__(self) -> None:
        self._ptr = _rlock_new()

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        return _rlock_acquire(self._ptr) == 0

    def release(self) -> None:
        _rlock_release(self._ptr)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return None


class Event:
    def __init__(self) -> None:
        self._ptr = _event_new()

    def set(self) -> None:
        _event_set(self._ptr)

    def clear(self) -> None:
        _event_clear(self._ptr)

    def is_set(self) -> bool:
        return _event_is_set(self._ptr) != 0

    def wait(self, timeout=None) -> bool:
        return _event_wait(self._ptr) == 0


class Thread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None) -> None:
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon
        self._native = None
        self._started = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("threads can only be started once")
        # The runtime calls a pcc callable with an args tuple; Thread.run
        # calls target(*args, **kwargs), so bind the keywords first.
        target = self._target
        if self._kwargs and target is not None:
            target = functools.partial(target, **self._kwargs)
        self._native = _thread_new(target, self._args)
        if _thread_start(self._native) != 0:
            raise RuntimeError("native Thread.start failed")
        self._started = True

    def join(self, timeout=None) -> None:
        if self._native is not None:
            _thread_join(self._native)
        return None

    def is_alive(self) -> bool:
        return self._native is not None and _thread_is_alive(self._native) != 0


class Condition:
    def __init__(self, lock=None) -> None:
        # Keep this shim monomorphic for pcc codegen. Passing the optional
        # lock object through would make ``lock or Lock()`` infer as DynType
        # and route ``self._lock.acquire(*args)`` through py_cpy_*.
        self._ptr = _condition_new(None)

    def acquire(self, blocking: bool = True, timeout: float = -1.0) -> bool:
        return _condition_acquire(self._ptr) == 0

    def release(self):
        _condition_release(self._ptr)

    def wait(self, timeout=None):
        return _condition_wait_timeout(self._ptr, timeout) == 0

    def notify(self, n: int = 1) -> None:
        _condition_notify(self._ptr)

    def notify_all(self) -> None:
        _condition_notify_all(self._ptr)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return None


class Semaphore:
    def __init__(self, value: int = 1) -> None:
        self._ptr = _sem_new(value)

    def acquire(self, blocking: bool = True, timeout=None) -> bool:
        return _sem_acquire(self._ptr) == 0

    def release(self, n: int = 1) -> None:
        while n > 0:
            _sem_release(self._ptr)
            n -= 1

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return None


class local:
    """Native-friendly threading.local implementation.

    Uses only an explicit ``get`` / ``set`` / ``delete`` API rather than
    Python's attribute-syntax descriptor protocol — pcc's no-libpython
    pipeline does not yet route ``__getattr__`` / ``__setattr__`` through
    native dispatch, so the dunder-driven variant of this class would
    silently force the link step back onto libpython. Per-thread storage
    is keyed on ``get_ident()`` from this same module.
    """

    def __init__(self) -> None:
        self._storage: dict = {}

    def _dict(self) -> dict:
        tid = get_ident()
        d = self._storage.get(tid)
        if d is None:
            d = {}
            self._storage[tid] = d
        return d

    def get(self, name: str, default=None):
        d = self._dict()
        if name in d:
            return d[name]
        return default

    def set(self, name: str, value) -> None:
        self._dict()[name] = value

    def delete(self, name: str) -> None:
        d = self._dict()
        if name in d:
            del d[name]
            return
        raise AttributeError(name)
