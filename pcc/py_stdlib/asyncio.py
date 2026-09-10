"""pcc.py_stdlib.asyncio -- minimal native asyncio surface.

This module intentionally covers the object model used by pcc's
no-libpython path: coroutine driving, futures/tasks, events, queues,
streams, and protocol/transport base classes. The TCP stream support is
blocking and deliberately small; it exists so native no-libpython programs can
serve simple stream workloads without falling back to CPython.
"""
from __future__ import annotations

import time as _time
import contextvars as _contextvars
from heapq import heappush as _heappush, heappop as _heappop

from pcc.extern import extern, c_int64, c_ptr, c_obj
from pcc.unsafe import is_tagged_int, load_i32, ptr_is_null


_py_await_iterator = extern("py_await_iterator", (c_ptr,), c_obj)
_py_await_step = extern("py_await_step", (c_ptr, c_ptr, c_ptr), c_obj)

_py_await: "extern" = extern("py_await", (c_ptr,), c_obj)
_py_asyncio_sleep: "extern" = extern("py_asyncio_sleep", (c_ptr,), c_obj)
_py_coroutine_args: "extern" = extern("py_coroutine_get_args", (c_ptr,), c_obj)
_py_task_new: "extern" = extern("py_task_new", (c_ptr,), c_obj)
_py_task_step: "extern" = extern("py_task_step", (c_ptr,), c_obj)
_tcp_listen: "extern" = extern("py_asyncio_tcp_listen", (c_ptr, c_ptr, c_int64), c_obj)
_tcp_accept: "extern" = extern("py_asyncio_tcp_accept", (c_ptr,), c_obj)
_tcp_connect: "extern" = extern("py_asyncio_tcp_connect", (c_ptr, c_ptr), c_obj)
_fd_recv: "extern" = extern("py_asyncio_fd_recv", (c_ptr, c_int64), c_obj)
_fd_send_all: "extern" = extern("py_asyncio_fd_send_all", (c_ptr, c_ptr), c_int64)
_fd_relay: "extern" = extern("py_asyncio_fd_relay", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
_fd_relay_step: "extern" = extern("py_asyncio_fd_relay_step", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_obj)
_fd_relay_step_last_progress: "extern" = extern("py_asyncio_fd_relay_step_last_progress", (), c_obj)
_fd_close: "extern" = extern("py_asyncio_fd_close", (c_ptr,), c_int64)
_fd_sockname: "extern" = extern("py_asyncio_fd_sockname", (c_ptr,), c_obj)
_fd_peername: "extern" = extern("py_asyncio_fd_peername", (c_ptr,), c_obj)
_io_waitset_backend: "extern" = extern(
    "py_asyncio_io_waitset_backend", (), c_obj
)
_usleep: "extern" = extern("usleep", (c_int64,), c_int64)


class CancelledError(BaseException):
    pass


class TimeoutError(Exception):
    pass


class IncompleteReadError(EOFError):
    def __init__(self, partial=None, expected=None) -> None:
        super().__init__("incomplete read")
        self.partial = partial
        self.expected = expected


class LimitOverrunError(Exception):
    def __init__(self, message="", consumed=0) -> None:
        super().__init__(message)
        self.consumed = consumed


_TASKS = []
_LOOP_BOX = [None]
_RUNNING_LOOP_BOX = [None]
_SERVERS = []
_PENDING_STREAM_RELAYS = []
# Active relays driven cooperatively by the event loop. Each entry is a mutable
# list [task_a, task_b, fd1_in, fd1_out, fd2_in, fd2_out, active_mask]; the mask
# is updated in place each step and the entry is dropped when it reaches 0.
_ACTIVE_RELAYS = []


def _is_none(value):
    if ptr_is_null(value):
        return True
    if is_tagged_int(value):
        return False
    return load_i32(value, 8) == 0


def _bytes_find(data, needle):
    needle_len = len(needle)
    if needle_len == 0:
        return 0
    data_len = len(data)
    if needle_len > data_len:
        return -1
    last = data_len - needle_len
    i = 0
    while i <= last:
        if data[i] == needle[0]:
            same = True
            j = 1
            while j < needle_len:
                if data[i + j] != needle[j]:
                    same = False
                    break
                j += 1
            if same:
                return i
        i += 1
    return -1


class InvalidStateError(Exception):
    pass


class Future:
    def __init__(self, *, loop=None) -> None:
        self._loop = get_event_loop() if loop is None else loop
        self._done = False
        self._cancelled = False
        self._result = None
        self._exception = None
        self._callbacks = []
        self._awaited_by = set()

    def get_loop(self):
        return self._loop

    def done(self):
        return self._done

    def cancelled(self):
        return self._cancelled

    def cancel(self, msg=None):
        if self._done:
            return False
        self._cancelled = True
        self._done = True
        self._exception = CancelledError(msg)
        self._run_callbacks()
        return True

    def set_result(self, value):
        if self._done:
            raise InvalidStateError('invalid state')
        self._result = value
        self._done = True
        self._run_callbacks()

    def set_exception(self, exc):
        if self._done:
            raise InvalidStateError('invalid state')
        if isinstance(exc, type):
            exc = exc()
        if not isinstance(exc, BaseException):
            raise TypeError('exception must be a BaseException')
        self._exception = exc
        self._done = True
        self._run_callbacks()

    def result(self):
        if self._cancelled:
            raise self._exception
        if not self._done:
            raise InvalidStateError('result is not ready')
        if self._exception is not None:
            raise self._exception
        return self._result

    def exception(self):
        if self._cancelled:
            raise self._exception
        if not self._done:
            raise InvalidStateError('exception is not ready')
        return self._exception

    def add_done_callback(self, callback, *, context=None):
        if context is None:
            context = _contextvars.copy_context()
        if self._done:
            self._loop.call_soon(callback, self, context=context)
        else:
            self._callbacks.append((callback, context))

    def remove_done_callback(self, callback):
        old = self._callbacks
        self._callbacks = [item for item in old if item[0] != callback]
        return len(old) - len(self._callbacks)

    def _run_callbacks(self):
        callbacks = self._callbacks
        self._callbacks = []
        for callback, context in callbacks:
            self._loop.call_soon(callback, self, context=context)

    def __await__(self):
        if not self._done:
            yield self
        if not self._done:
            raise RuntimeError('await was resumed before its Future completed')
        return self.result()


def _completed_future(value=None):
    future = Future()
    future.set_result(value)
    return future


def future_add_to_awaited_by(future, waiter):
    future._awaited_by.add(waiter)


def future_discard_from_awaited_by(future, waiter):
    future._awaited_by.discard(waiter)


def _advance_coroutine(task, error):
    return _py_await_step(task._coro, None, error)


class Task(Future):
    def __init__(self, coro, *, loop=None, name=None, context=None, eager_start=False) -> None:
        super().__init__(loop=loop)
        self._coro = _py_await_iterator(coro)
        self._source_coro = coro
        self._name = 'Task' if name is None else str(name)
        self._context = _contextvars.copy_context() if context is None else context
        self._waiter = None
        self._pending_error = None
        self._must_cancel = False
        self._cancel_message = None
        self._cancel_count = 0
        self._queued = False
        _TASKS.append(self)
        if eager_start and self._loop.is_running():
            self._step()
        else:
            self._schedule()

    def get_coro(self):
        return self._source_coro

    def get_name(self):
        return self._name

    def set_name(self, value):
        self._name = str(value)

    def get_context(self):
        return self._context

    def cancelling(self):
        return self._cancel_count

    def uncancel(self):
        if self._cancel_count > 0:
            self._cancel_count -= 1
        if self._cancel_count == 0:
            self._must_cancel = False
        return self._cancel_count

    def cancel(self, msg=None):
        if self._done:
            return False
        self._cancel_count += 1
        self._cancel_message = msg
        if self._waiter is not None and self._waiter.cancel(msg):
            return True
        self._must_cancel = True
        self._schedule()
        return True

    def set_result(self, value):
        raise RuntimeError('Task does not support set_result')

    def set_exception(self, error):
        raise RuntimeError('Task does not support set_exception')

    def _schedule(self):
        if not self._queued and not self._done:
            self._queued = True
            self._loop.call_soon(self._step, context=self._context)

    def _wakeup(self, future):
        if self._done:
            return
        self._waiter = None
        try:
            future.result()
        except BaseException as error:
            self._pending_error = error
        self._schedule()

    def _finish(self):
        self._waiter = None
        self._coro = None
        self._pending_error = None
        if self in _TASKS:
            _TASKS.remove(self)
        self._run_callbacks()

    def _step(self):
        self._queued = False
        if self._done:
            return
        error = self._pending_error
        self._pending_error = None
        if self._must_cancel:
            if not isinstance(error, CancelledError):
                error = CancelledError(self._cancel_message)
            self._must_cancel = False
        previous = self._loop._current_task
        self._loop._current_task = self
        try:
            yielded = _advance_coroutine(self, error)
        except StopIteration as stopped:
            self._result = stopped.value
            self._done = True
            self._finish()
            return
        except CancelledError as cancelled:
            self._exception = cancelled
            self._cancelled = True
            self._done = True
            self._finish()
            return
        except BaseException as failed:
            self._exception = failed
            self._done = True
            self._finish()
            if isinstance(failed, (KeyboardInterrupt, SystemExit)):
                raise
            return
        finally:
            self._loop._current_task = previous
        if yielded is None:
            self._schedule()
        elif isinstance(yielded, Future):
            if yielded is self:
                self._pending_error = RuntimeError('Task cannot await itself')
                self._schedule()
            elif yielded.get_loop() is not self._loop:
                self._pending_error = RuntimeError('Future belongs to another event loop')
                self._schedule()
            else:
                self._waiter = yielded
                yielded.add_done_callback(self._wakeup, context=self._context)
                if self._must_cancel and yielded.cancel(self._cancel_message):
                    self._must_cancel = False
        else:
            self._pending_error = RuntimeError('Task received an unsupported suspension value')
            self._schedule()

    @staticmethod
    def all_tasks(loop=None):
        return all_tasks(loop)


# TaskGroup state machine adapted from CPython 3.15 asyncio/taskgroups.py.
# Copyright Python Software Foundation; distributed under the PSF license.
class TaskGroup:
    """Asynchronous context manager for managing groups of

    Example use:

        async with asyncio.TaskGroup() as group:
            task1 = group.create_task(some_coroutine(...))
            task2 = group.create_task(other_coroutine(...))
        print("Both tasks have completed now.")

    All tasks are awaited when the context manager exits.

    Any exceptions other than `asyncio.CancelledError` raised within
    a task will cancel all remaining tasks and wait for them to exit.
    The exceptions are then combined and raised as an `ExceptionGroup`.
    """
    def __init__(self):
        self._entered = False
        self._exiting = False
        self._aborting = False
        self._loop = None
        self._parent_task = None
        self._parent_cancel_requested = False
        self._tasks = set()
        self._errors = []
        self._base_error = None
        self._on_completed_fut = None
        self._cancel_on_enter = False

    def __repr__(self):
        info = ['']
        if self._tasks:
            info.append(f'tasks={len(self._tasks)}')
        if self._errors:
            info.append(f'errors={len(self._errors)}')
        if self._aborting:
            info.append('cancelling')
        elif self._entered:
            info.append('entered')

        info_str = ' '.join(info)
        return f'<TaskGroup{info_str}>'

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError(
                f"TaskGroup {self!r} has already been entered")
        if self._loop is None:
            self._loop = get_running_loop()
        self._parent_task = current_task(self._loop)
        if self._parent_task is None:
            raise RuntimeError(
                f'TaskGroup {self!r} cannot determine the parent task')
        self._entered = True
        if self._cancel_on_enter:
            self.cancel()

        return self

    async def __aexit__(self, et, exc, tb):
        tb = None
        try:
            return await self._aexit(et, exc)
        finally:
            # Exceptions are heavy objects that can have object
            # cycles (bad for GC); let's not keep a reference to
            # a bunch of them. It would be nicer to use a try/finally
            # in __aexit__ directly but that introduced some diff noise
            self._parent_task = None
            self._errors = None
            self._base_error = None
            exc = None

    async def _aexit(self, et, exc):
        self._exiting = True

        if (exc is not None and
                self._is_base_error(exc) and
                self._base_error is None):
            self._base_error = exc

        if et is not None and issubclass(et, CancelledError):
            propagate_cancellation_error = exc
        else:
            propagate_cancellation_error = None

        if et is not None:
            if not self._aborting:
                # Our parent task is being cancelled:
                #
                #    async with TaskGroup() as g:
                #        g.create_task(...)
                #        await ...  # <- CancelledError
                #
                # or there's an exception in "async with":
                #
                #    async with TaskGroup() as g:
                #        g.create_task(...)
                #        1 / 0
                #
                self._abort()

        # We use while-loop here because "self._on_completed_fut"
        # can be cancelled multiple times if our parent task
        # is being cancelled repeatedly (or even once, when
        # our own cancellation is already in progress)
        while self._tasks:
            if self._on_completed_fut is None:
                self._on_completed_fut = self._loop.create_future()

            try:
                await self._on_completed_fut
            except CancelledError as ex:
                if not self._aborting:
                    # Our parent task is being cancelled:
                    #
                    #    async def wrapper():
                    #        async with TaskGroup() as g:
                    #            g.create_task(foo)
                    #
                    # "wrapper" is being cancelled while "foo" is
                    # still running.
                    propagate_cancellation_error = ex
                    self._abort()

            self._on_completed_fut = None

        assert not self._tasks

        if self._base_error is not None:
            try:
                raise self._base_error
            finally:
                exc = None

        if self._parent_cancel_requested:
            # If this flag is set we *must* call uncancel().
            if self._parent_task.uncancel() == 0:
                # If there are no pending cancellations left,
                # don't propagate CancelledError.
                propagate_cancellation_error = None

        # Propagate CancelledError if there is one, except if there
        # are other errors -- those have priority.
        try:
            if propagate_cancellation_error is not None and not self._errors:
                try:
                    raise propagate_cancellation_error
                finally:
                    exc = None
        finally:
            propagate_cancellation_error = None

        if et is not None and not issubclass(et, CancelledError):
            self._errors.append(exc)

        if self._errors:
            # If the parent task is being cancelled from the outside
            # of the taskgroup, un-cancel and re-cancel the parent task,
            # which will keep the cancel count stable.
            if self._parent_task.cancelling():
                self._parent_task.uncancel()
                self._parent_task.cancel()
            try:
                # If the *only* error is a GeneratorExit from the body
                # of the group, then instead of raising an
                # ExceptionGroup we raise GeneratorExit. This ensures
                # that async generators that use TaskGroup properly
                # swallow the exception on `aclose()` while ensuring
                # that no exceptions from subtasks are swallowed.
                if (
                    et is not None
                    and issubclass(et, GeneratorExit)
                    and len(self._errors) == 1
                ):
                    raise exc
                else:
                    raise BaseExceptionGroup(
                        'unhandled errors in a TaskGroup',
                        self._errors,
                    ) from None
            finally:
                exc = None

        # Suppress any remaining exception (exceptions deserving to be raised
        # were raised above).
        return True

    def create_task(self, coro, *, name=None, context=None, eager_start=False):
        """Create a new task in this group and return it.

        Similar to `asyncio.create_task`. The keywords are written out rather
        than forwarded as `**kwargs` because a `**` expansion at a call site
        needs a statically dict-typed operand here, and a `**kwargs` parameter
        is not one -- codegen refuses it with "arbitrary mapping expansion is
        not yet source-ordered". Naming them also matches the loop method this
        forwards to and CPython's own keyword-only signature.
        """
        if not self._entered:
            coro.close()
            raise RuntimeError(f"TaskGroup {self!r} has not been entered")
        if self._exiting and not self._tasks:
            coro.close()
            raise RuntimeError(f"TaskGroup {self!r} is finished")
        if self._aborting:
            coro.close()
            raise RuntimeError(f"TaskGroup {self!r} is shutting down")
        task = self._loop.create_task(
            coro, name=name, context=context, eager_start=eager_start
        )

        future_add_to_awaited_by(task, self._parent_task)

        # Always schedule the done callback even if the task is
        # already done (e.g. if the coro was able to complete eagerly),
        # otherwise if the task completes with an exception then it will cancel
        # the current task too early. gh-128550, gh-128588
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        try:
            return task
        finally:
            # gh-128552: prevent a refcycle of
            # task.exception().__traceback__->TaskGroup.create_task->task
            del task

    # Since Python 3.8 Tasks propagate all exceptions correctly,
    # except for KeyboardInterrupt and SystemExit which are
    # still considered special.

    def _is_base_error(self, exc: BaseException) -> bool:
        assert isinstance(exc, BaseException)
        return isinstance(exc, (SystemExit, KeyboardInterrupt))

    def _abort(self):
        self._aborting = True

        for t in self._tasks:
            if not t.done():
                t.cancel()

    def _on_task_done(self, task):
        self._tasks.discard(task)

        future_discard_from_awaited_by(task, self._parent_task)

        if self._on_completed_fut is not None and not self._tasks:
            if not self._on_completed_fut.done():
                self._on_completed_fut.set_result(True)

        if task.cancelled():
            return

        exc = task.exception()
        if exc is None:
            return

        self._errors.append(exc)
        if self._is_base_error(exc) and self._base_error is None:
            self._base_error = exc

        if self._parent_task.done():
            # Not sure if this case is possible, but we want to handle
            # it anyways.
            self._loop.call_exception_handler({
                'message': f'Task {task!r} has errored out but its parent '
                           f'task {self._parent_task} is already completed',
                'exception': exc,
                'task': task,
            })
            return

        if not self._aborting and not self._parent_cancel_requested:
            # If parent task *is not* being cancelled, it means that we want
            # to manually cancel it to abort whatever is being run right now
            # in the TaskGroup.  But we want to mark parent task as
            # "not cancelled" later in __aexit__.  Example situation that
            # we need to handle:
            #
            #    async def foo():
            #        try:
            #            async with TaskGroup() as g:
            #                g.create_task(crash_soon())
            #                await something  # <- this needs to be canceled
            #                                 #    by the TaskGroup, e.g.
            #                                 #    foo() needs to be cancelled
            #        except Exception:
            #            # Ignore any exceptions raised in the TaskGroup
            #            pass
            #        await something_else     # this line has to be called
            #                                 # after TaskGroup is finished.
            self._abort()
            self._parent_cancel_requested = True
            self._parent_task.cancel()

    def cancel(self):
        """Cancel the task group

        `cancel()` will be called on any tasks in the group that aren't yet
        done, as well as the parent (body) of the group.  This will cause
        the task group context manager to exit *without*
        `asyncio.CancelledError` being raised.

        If `cancel()` is called before entering the task group, the group
        will be cancelled upon entry.  This is useful for patterns where
        one piece of code passes an unused TaskGroup instance to another in
        order to have the ability to cancel anything run within the group.

        `cancel()` is idempotent and may be called after the task group has
        already exited.
        """
        if not self._entered:
            self._cancel_on_enter = True
            return
        if self._exiting and not self._tasks:
            return
        if not self._aborting:
            self._abort()
            if self._parent_task and not self._parent_cancel_requested:
                self._parent_cancel_requested = True
                self._parent_task.cancel()


class Event:
    def __init__(self) -> None:
        self._flag = False

    def is_set(self):
        return self._flag

    def set(self):
        self._flag = True

    def clear(self):
        self._flag = False

    def wait(self):
        return _completed_future(self._flag)


class Queue:
    def __init__(self, maxsize=0) -> None:
        self._items = []
        self._maxsize = maxsize
        self._read_index = 0

    def qsize(self):
        return len(self._items) - self._read_index

    def empty(self):
        return self.qsize() == 0

    def full(self):
        return self._maxsize > 0 and self.qsize() >= self._maxsize

    def put_nowait(self, item):
        self._items.append(item)

    def put(self, item):
        self.put_nowait(item)
        return _completed_future(None)

    def get_nowait(self):
        if self._read_index < len(self._items):
            item = self._items[self._read_index]
            self._read_index += 1
            return item
        return None

    def get(self):
        return _completed_future(self.get_nowait())

    def task_done(self):
        return None

    def join(self):
        return _completed_future(None)


class _ByteBuffer:
    def __init__(self) -> None:
        self._data = b""

    def __len__(self):
        return len(self._data)

    def __getitem__(self, key):
        return self._data[key]

    def __setitem__(self, key, value):
        if len(self._data) == 0:
            self._data = value
        else:
            self._data = value + self._data

    def extend(self, data):
        if len(self._data) == 0:
            self._data = data
        else:
            self._data = self._data + data

    def prepend(self, data):
        if not data:
            return None
        if len(self._data) == 0:
            self._data = data
        else:
            self._data = data + self._data
        return None

    def take(self, n):
        if n is None or n < 0 or n >= len(self._data):
            data = self._data
            self._data = b""
            return data
        data = self._data[:n]
        self._data = self._data[n:]
        return data

    def take_until(self, sep):
        idx = _bytes_find(self._data, sep)
        if idx < 0:
            return self.take(-1)
        end = idx + len(sep)
        return self.take(end)


class StreamReader:
    def __init__(self, *args, **kwargs) -> None:
        self._buffer = _ByteBuffer()
        self._eof = False
        self._transport = None
        self._exception = None

    def set_transport(self, transport):
        self._transport = transport

    def feed_data(self, data):
        self._buffer.extend(data)

    def feed_eof(self):
        self._eof = True

    def set_exception(self, exc):
        self._exception = exc

    def exception(self):
        return self._exception

    def at_eof(self):
        return self._eof and len(self._buffer) == 0

    def _fd(self):
        if self._transport is None:
            return None
        return self._transport._fd

    def _fill_once(self, n=65536):
        fd = self._fd()
        if _is_none(fd) or self._eof:
            return b""
        if n is None or n <= 0:
            n = 65536
        data = _fd_recv(fd, n)
        if len(data) == 0:
            self._eof = True
        else:
            self._buffer.extend(data)
        return data

    def read(self, n=-1):
        if self._exception is not None:
            raise self._exception
        if len(self._buffer) == 0 and not self._eof:
            size = n
            if size is None or size <= 0:
                size = 65536
            self._fill_once(size)
        return _completed_future(self._buffer.take(n))

    def readexactly(self, n):
        if self._exception is not None:
            raise self._exception
        while len(self._buffer) < n and not self._eof:
            self._fill_once(n - len(self._buffer))
        data = self._buffer.take(n)
        if len(data) < n:
            raise IncompleteReadError(data, n)
        return _completed_future(data)

    def read_n(self, n):
        return self.readexactly(n)

    def read_w(self, n):
        if self._exception is not None:
            raise self._exception
        while len(self._buffer) < n and not self._eof:
            data = self._fill_once(n - len(self._buffer))
            if len(data) == 0:
                break
        return _completed_future(self._buffer.take(n))

    def rollback(self, data):
        self._buffer.prepend(data)
        return None

    def readuntil(self, separator=b"\n"):
        if self._exception is not None:
            raise self._exception
        while _bytes_find(self._buffer._data, separator) < 0 and not self._eof:
            self._fill_once(4096)
        return _completed_future(self._buffer.take_until(separator))

    def read_until(self, separator=b"\n"):
        return self.readuntil(separator)

    def readline(self):
        return self.readuntil(b"\n")


class Transport:
    def __init__(self, extra=None) -> None:
        self._extra = extra or {}
        self._closed = False
        self._protocol = None

    def is_closing(self):
        return self._closed

    def close(self):
        self._closed = True

    def abort(self):
        self.close()

    def write(self, data):
        return None

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def write_eof(self):
        return None

    def can_write_eof(self):
        return False

    def get_extra_info(self, name, default=None):
        return self._extra.get(name, default)

    def set_protocol(self, protocol):
        self._protocol = protocol

    def get_protocol(self):
        return self._protocol

    def set_write_buffer_limits(self, high=None, low=None):
        return None

    def get_write_buffer_size(self):
        return 0

    def pause_reading(self):
        return None

    def resume_reading(self):
        return None


class _Socket:
    def __init__(self, fd) -> None:
        self._fd = fd
        self._closed = False
        self.family = "AddressFamily.AF_INET"

    def fileno(self):
        return self._fd

    def getsockname(self):
        return _fd_sockname(self._fd)

    def getpeername(self):
        return _fd_peername(self._fd)

    def getsockopt(self, level, option, buflen=None):
        return b""

    def close(self):
        if not self._closed:
            self._closed = True
            _fd_close(self._fd)


class _SocketTransport(Transport):
    def __init__(self, fd, sock) -> None:
        super().__init__(
            {
                "socket": sock,
                "sockname": sock.getsockname(),
                "peername": sock.getpeername(),
            }
        )
        self._fd = fd
        self._socket = sock

    def close(self):
        if not self._closed:
            self._closed = True
            self._socket.close()

    def write(self, data):
        if self._closed:
            return None
        _fd_send_all(self._fd, data)
        return None


class StreamWriter:
    def __init__(self, transport=None, protocol=None, reader=None, loop=None) -> None:
        self._transport = transport or Transport()
        self._protocol = protocol
        self._reader = reader
        self._loop = loop

    def write(self, data):
        return self._transport.write(data)

    def writelines(self, data):
        return self._transport.writelines(data)

    def write_eof(self):
        return self._transport.write_eof()

    def can_write_eof(self):
        return self._transport.can_write_eof()

    def get_extra_info(self, name, default=None):
        return self._transport.get_extra_info(name, default)

    def is_closing(self):
        return self._transport.is_closing()

    def close(self):
        return self._transport.close()

    def drain(self):
        return _completed_future(None)

    def wait_closed(self):
        return _completed_future(None)


def _stream_relay_endpoint(awaitable):
    if ptr_is_null(awaitable) or is_tagged_int(awaitable):
        return None
    if load_i32(awaitable, 8) != 20:
        return None
    args = _py_coroutine_args(awaitable)
    if _is_none(args):
        return None
    i = 0
    n = len(args)
    while i + 1 < n:
        reader = args[i]
        writer = args[i + 1]
        if isinstance(reader, StreamReader) and isinstance(writer, StreamWriter):
            transport = writer._transport
            if isinstance(transport, _SocketTransport):
                read_fd = reader._fd()
                write_fd = transport._fd
                if not _is_none(read_fd) and not _is_none(write_fd):
                    return reader, writer, read_fd, write_fd
        i += 1
    return None


def _mark_task_done(task, result=None):
    task._result = result
    task._done = True


def _try_stream_relay(awaitable, task):
    endpoint = _stream_relay_endpoint(awaitable)
    if endpoint is None:
        return False
    i = 0
    while i < len(_PENDING_STREAM_RELAYS):
        pending_task, pending_endpoint = _PENDING_STREAM_RELAYS[i]
        if pending_task.done():
            _PENDING_STREAM_RELAYS.pop(i)
            continue
        if pending_endpoint[2] == endpoint[3] and endpoint[2] == pending_endpoint[3]:
            _PENDING_STREAM_RELAYS.pop(i)
            # Register the relay for cooperative, non-blocking driving by the
            # event loop instead of taking over the single thread with a
            # blocking full-lifetime relay. The two short-circuited channel
            # tasks are marked done immediately (the loop never has to run their
            # Python bodies); the actual byte forwarding now happens in
            # _drive_relays(), so one open/idle connection no longer freezes
            # every other connection on the loop.
            _ACTIVE_RELAYS.append(
                [
                    pending_task,
                    task,
                    pending_endpoint[2],
                    pending_endpoint[3],
                    endpoint[2],
                    endpoint[3],
                    3,
                ]
            )
            _mark_task_done(pending_task)
            _mark_task_done(task)
            return True
        i += 1
    _PENDING_STREAM_RELAYS.append((task, endpoint))
    return False


def _drive_relays():
    # One non-blocking forwarding pass over every active relay. The native step
    # returns None once a relay is finished (its fds are already closed); while a
    # relay is still open it returns an updated mask (a small Python int) that we
    # carry back into the next step. Finished relays mark their two short-circuit
    # tasks done and are dropped. Returns True when a relay finished so the loop
    # knows it made progress this pass.
    progressed = False
    i = 0
    while i < len(_ACTIVE_RELAYS):
        relay = _ACTIVE_RELAYS[i]
        result = _fd_relay_step(relay[2], relay[3], relay[4], relay[5], relay[6])
        if _is_none(result):
            _mark_task_done(relay[0])
            _mark_task_done(relay[1])
            _ACTIVE_RELAYS.pop(i)
            progressed = True
            continue
        if not _is_none(_fd_relay_step_last_progress()):
            progressed = True
        relay[6] = result
        i += 1
    return progressed


class Protocol:
    def connection_made(self, transport):
        self.transport = transport

    def connection_lost(self, exc):
        return None

    def data_received(self, data):
        return None

    def eof_received(self):
        return None

    def get_buffer(self, sizehint):
        return bytearray(sizehint)

    def buffer_updated(self, nbytes):
        return None


class DatagramProtocol(Protocol):
    def datagram_received(self, data, addr):
        return None

    def error_received(self, exc):
        return None


class _AppTransport(Transport):
    def write(self, data):
        return None


class _SSLProtocol(Protocol):
    def __init__(
        self,
        loop=None,
        app_protocol=None,
        sslcontext=None,
        waiter=None,
        server_side=False,
        server_hostname=None,
        call_connection_made=True,
    ) -> None:
        self._loop = loop
        self._app_protocol = app_protocol
        self._sslcontext = sslcontext
        self._waiter = waiter
        self._server_side = server_side
        self._server_hostname = server_hostname
        self._app_transport = _AppTransport()
        self._closed = False
        if call_connection_made and app_protocol is not None:
            app_protocol.connection_made(self._app_transport)

    def connection_made(self, transport):
        self._transport = transport
        if self._app_protocol is not None:
            self._app_protocol.connection_made(self._app_transport)

    def connection_lost(self, exc):
        self._closed = True
        self._app_transport._closed = True
        if self._app_protocol is not None:
            self._app_protocol.connection_lost(exc)

    def data_received(self, data):
        if self._app_protocol is not None:
            self._app_protocol.data_received(data)

    def eof_received(self):
        self._closed = True
        self._app_transport._closed = True
        if self._app_protocol is not None:
            return self._app_protocol.eof_received()
        return None

    def get_buffer(self, sizehint):
        return bytearray(sizehint)

    def buffer_updated(self, nbytes):
        return None


class _SSLProtoModule:
    SSLProtocol = _SSLProtocol


sslproto = _SSLProtoModule()


class _Handle:
    def __init__(self, callback, args, context):
        self._callback = callback
        self._args = args
        self._context = _contextvars.copy_context() if context is None else context
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self._callback = None
        self._args = ()

    def cancelled(self):
        return self._cancelled

    def _run(self):
        if not self._cancelled:
            self._context.run(self._callback, *self._args)


class _Loop:
    def __init__(self) -> None:
        self._closed = False
        self._running = False
        self._stopping = False
        self._current_task = None
        self._ready = []
        self._timers = []
        self._timer_sequence = 0
        self._exception_handler = None
        self._task_factory = None

    def create_future(self):
        return Future(loop=self)

    def create_task(self, coro, *, name=None, context=None, eager_start=False):
        if self._closed:
            raise RuntimeError('event loop is closed')
        if self._task_factory is not None:
            return self._task_factory(self, coro, name=name, context=context, eager_start=eager_start)
        return Task(coro, loop=self, name=name, context=context, eager_start=eager_start)

    def set_task_factory(self, factory):
        self._task_factory = factory

    def get_task_factory(self):
        return self._task_factory

    def run_until_complete(self, awaitable):
        if self._closed or self._running:
            raise RuntimeError('event loop is closed or already running')
        future = ensure_future(awaitable, loop=self)
        previous = _RUNNING_LOOP_BOX[0]
        _RUNNING_LOOP_BOX[0] = self
        self._running = True
        self._stopping = False
        try:
            while not future.done():
                if self._stopping:
                    raise RuntimeError('event loop stopped before Future completed')
                self._run_once()
            return future.result()
        finally:
            self._running = False
            _RUNNING_LOOP_BOX[0] = previous

    def _run_once(self):
        now = self.time()
        while self._timers and self._timers[0][2].cancelled():
            _heappop(self._timers)
        if not self._ready:
            delay = 0.001
            if self._timers:
                delay = max(0.0, self._timers[0][0] - now)
                if _SERVERS or _ACTIVE_RELAYS:
                    delay = min(delay, 0.001)
            if delay > 0:
                _time.sleep(delay)
            now = self.time()
        while self._timers and self._timers[0][0] <= now:
            record = _heappop(self._timers)
            if not record[2].cancelled():
                self._ready.append(record[2])
        ready = self._ready
        self._ready = []
        for handle in ready:
            try:
                handle._run()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as error:
                self.call_exception_handler({'message': 'exception in event loop callback', 'exception': error})
        for server in _SERVERS:
            if not server._closed:
                server._accept_once()
        _drive_relays()

    def run_forever(self):
        if self._running or self._closed:
            raise RuntimeError('event loop is closed or already running')
        previous = _RUNNING_LOOP_BOX[0]
        _RUNNING_LOOP_BOX[0] = self
        self._running = True
        self._stopping = False
        try:
            while not self._stopping:
                self._run_once()
        finally:
            self._running = False
            _RUNNING_LOOP_BOX[0] = previous

    def stop(self):
        self._stopping = True

    def close(self):
        if self._running:
            raise RuntimeError('cannot close a running event loop')
        self._closed = True
        self._ready = []
        self._timers = []

    def is_closed(self):
        return self._closed

    def is_running(self):
        return self._running

    def shutdown_asyncgens(self):
        return _completed_future(None)

    def call_soon(self, callback, *args, context=None):
        if self._closed:
            raise RuntimeError('event loop is closed')
        handle = _Handle(callback, args, context)
        self._ready.append(handle)
        return handle

    def call_at(self, when, callback, *args, context=None):
        if self._closed:
            raise RuntimeError('event loop is closed')
        handle = _Handle(callback, args, context)
        self._timer_sequence += 1
        _heappush(self._timers, (when, self._timer_sequence, handle))
        return handle

    def call_later(self, delay, callback, *args, context=None):
        return self.call_at(self.time() + max(0.0, delay), callback, *args, context=context)

    def time(self):
        return _time.monotonic()

    def set_exception_handler(self, handler):
        self._exception_handler = handler

    def call_exception_handler(self, details):
        if self._exception_handler is not None:
            self._exception_handler(self, details)
        else:
            error = details.get('exception')
            if error is not None:
                raise error

    def add_reader(self, fd, callback, *args):
        raise NotImplementedError('reader callbacks require the native I/O adapter')

    def remove_reader(self, fd):
        return False

    def run_in_executor(self, executor, fn, *args):
        raise NotImplementedError('executor integration is not implemented')

    def getaddrinfo(self, *args, **kwargs):
        raise NotImplementedError('asyncio.getaddrinfo awaits native event-loop I/O')

    def create_datagram_endpoint(self, *args, **kwargs):
        raise NotImplementedError('asyncio.create_datagram_endpoint awaits native event-loop I/O')

    def create_connection(self, *args, **kwargs):
        raise NotImplementedError('asyncio.create_connection awaits native event-loop I/O')


class _Server:
    def __init__(self, loop, fd, handler) -> None:
        self._loop = loop
        self._fd = fd
        self._handler = handler
        self._closed = False
        self.sockets = [_Socket(fd)]

    def close(self):
        if not self._closed:
            self._closed = True
            for sock in self.sockets:
                sock.close()

    def wait_closed(self):
        return _completed_future(None)

    def _accept_once(self):
        client_fd = _tcp_accept(self._fd)
        if _is_none(client_fd):
            return False
        sock = _Socket(client_fd)
        transport = _SocketTransport(client_fd, sock)
        reader = StreamReader()
        reader.set_transport(transport)
        writer = StreamWriter(transport, None, reader, self._loop)
        result = self._handler(reader, writer)
        if not _is_none(result):
            _py_await(result)
        return True


def get_event_loop():
    loop = _LOOP_BOX[0]
    if _is_none(loop):
        loop = _Loop()
        _LOOP_BOX[0] = loop
    return loop


def new_event_loop():
    return _Loop()


def io_waitset_backend():
    """Name of the IO-waitset readiness backend this platform provides.

    Returns "kqueue" on Darwin/BSD (the scalable kevent(2) notifier in
    py_io_waitset.c) and "poll" elsewhere (the level-triggered poll fallback).
    Lets the event loop pick the notifier over the O(n) poll rescan.
    """
    return _io_waitset_backend()


def set_event_loop(loop):
    _LOOP_BOX[0] = loop


def get_running_loop():
    loop = _RUNNING_LOOP_BOX[0]
    if loop is None:
        raise RuntimeError('no running event loop')
    return loop


def run(awaitable, *, debug=None, loop_factory=None):
    if _RUNNING_LOOP_BOX[0] is not None:
        raise RuntimeError('asyncio.run cannot be called from a running event loop')
    loop = _Loop() if loop_factory is None else loop_factory()
    previous = _LOOP_BOX[0]
    _LOOP_BOX[0] = loop
    try:
        return loop.run_until_complete(awaitable)
    finally:
        pending = [task for task in _TASKS if task.get_loop() is loop and not task.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                loop.run_until_complete(task)
            except BaseException:
                pass
        loop.close()
        _LOOP_BOX[0] = previous


class _YieldOnce:
    def __await__(self):
        yield None
        return None


def _set_result_unless_cancelled(future, value):
    if not future.done():
        future.set_result(value)


async def sleep(delay, result=None):
    if delay <= 0:
        await _YieldOnce()
        return result
    loop = get_running_loop()
    future = loop.create_future()
    handle = loop.call_later(delay, _set_result_unless_cancelled, future, result)
    try:
        return await future
    finally:
        handle.cancel()


async def _wrap_awaitable(awaitable):
    return await awaitable


def ensure_future(awaitable, *, loop=None):
    if isinstance(awaitable, Future):
        if loop is not None and awaitable.get_loop() is not loop:
            raise ValueError('Future belongs to a different loop')
        return awaitable
    if loop is None:
        loop = get_event_loop()
    return loop.create_task(awaitable)


def create_task(coro, *, name=None, context=None, eager_start=False):
    return get_running_loop().create_task(coro, name=name, context=context, eager_start=eager_start)


def all_tasks(loop=None):
    if loop is None:
        loop = get_running_loop()
    return set(task for task in _TASKS if task.get_loop() is loop and not task.done())


def current_task(loop=None):
    if loop is None:
        loop = get_running_loop()
    return loop._current_task


async def wait_for(awaitable, timeout=None):
    task = ensure_future(awaitable)
    if timeout is None:
        return await task
    loop = get_running_loop()
    timed_out = [False]
    def expire():
        if not task.done():
            timed_out[0] = True
            task.cancel()
    handle = loop.call_later(timeout, expire)
    try:
        return await task
    except CancelledError:
        if timed_out[0]:
            raise TimeoutError()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise
    finally:
        handle.cancel()


FIRST_COMPLETED = 'FIRST_COMPLETED'
FIRST_EXCEPTION = 'FIRST_EXCEPTION'
ALL_COMPLETED = 'ALL_COMPLETED'


async def wait(fs, *, timeout=None, return_when=ALL_COMPLETED):
    if return_when not in (FIRST_COMPLETED, FIRST_EXCEPTION, ALL_COMPLETED):
        raise ValueError('invalid return_when')
    children = set(fs)
    if not children:
        raise ValueError('empty task set')
    waiter = get_running_loop().create_future()
    def done(task):
        finished = [item for item in children if item.done()]
        if (len(finished) == len(children) or return_when == FIRST_COMPLETED
                or (return_when == FIRST_EXCEPTION and not task.cancelled() and task.exception() is not None)):
            _set_result_unless_cancelled(waiter, None)
    for task in children:
        task.add_done_callback(done)
    timer = None
    if timeout is not None:
        timer = get_running_loop().call_later(timeout, _set_result_unless_cancelled, waiter, None)
    try:
        await waiter
    finally:
        if timer is not None:
            timer.cancel()
        for task in children:
            task.remove_done_callback(done)
    return set(task for task in children if task.done()), set(task for task in children if not task.done())


async def gather(*aws, return_exceptions=False):
    children = [ensure_future(aw) for aw in aws]
    out = []
    try:
        for child in children:
            try:
                # Bound first rather than `out.append(await child)`. An await
                # in argument position suspends after the receiver has already
                # been evaluated, and the receiver is not spilled to the
                # generator frame across the suspension, so the self backend
                # rejects the resume block with "definition of 'out' does not
                # dominate await.completed". Compiler gap, reproduced in 16
                # lines; see the receipt in pcc-gateway
                # benchmarks/results/2026-09-10-ownership-leaks/README.md.
                item = await child
                out.append(item)
            except BaseException as error:
                if not return_exceptions:
                    raise
                out.append(error)
        return out
    except CancelledError:
        for child in children:
            child.cancel()
        for child in children:
            try:
                await child
            except BaseException:
                pass
        raise


def open_connection(*args, **kwargs):
    host = None
    port = None
    if len(args) > 0:
        host = args[0]
    if len(args) > 1:
        port = args[1]
    if "host" in kwargs:
        host = kwargs.get("host")
    if "port" in kwargs:
        port = kwargs.get("port")
    fd = _tcp_connect(host, port)
    if _is_none(fd):
        raise OSError("asyncio.open_connection failed")
    sock = _Socket(fd)
    transport = _SocketTransport(fd, sock)
    reader = StreamReader()
    reader.set_transport(transport)
    writer = StreamWriter(transport, None, reader, get_event_loop())
    return _completed_future((reader, writer))


async def open_unix_connection(*args, **kwargs):
    raise NotImplementedError(
        "asyncio.open_unix_connection awaits native event-loop I/O"
    )


def start_server(*args, **kwargs):
    if len(args) == 0:
        client_connected_cb = kwargs.get("client_connected_cb")
    else:
        client_connected_cb = args[0]
    host = kwargs.get("host")
    port = kwargs.get("port")
    if len(args) > 1:
        host = args[1]
    if len(args) > 2:
        port = args[2]
    reuse_port = 1 if kwargs.get("reuse_port") else 0
    fd = _tcp_listen(host, port, reuse_port)
    if _is_none(fd):
        raise OSError("asyncio.start_server failed")
    loop = get_event_loop()
    server = _Server(loop, fd, client_connected_cb)
    _SERVERS.append(server)
    return _completed_future(server)


async def start_unix_server(*args, **kwargs):
    raise NotImplementedError("asyncio.start_unix_server awaits native event-loop I/O")
