"""pcc.py_stdlib.subprocess — narrow ``subprocess`` skeleton.

Scope: ``run`` / ``check_output`` / ``check_call`` / ``Popen`` shape.
Implementation relies on extern ``posix_spawn`` + ``waitpid`` + pipe
FD plumbing; the interpreted fallback here uses CPython's real
subprocess so development proceeds even before the extern bindings
land.
"""

from __future__ import annotations

from pcc.extern import extern, c_int, c_int64, c_ptr, c_obj

py_subprocess_check_output = extern(
    "py_subprocess_check_output",
    (c_ptr,),
    c_obj,
)
py_subprocess_run = extern(
    "py_subprocess_run",
    (c_ptr, c_int),
    c_int64,
)
py_subprocess_run_timeout = extern(
    "py_subprocess_run_timeout",
    (c_ptr, c_int, c_int64),
    c_int64,
)
py_subprocess_run_env = extern(
    "py_subprocess_run_env",
    (c_ptr, c_int, c_obj),
    c_int64,
)

_TIMEOUT_RETURN_CODE = -124


class CalledProcessError(Exception):
    def __init__(self, returncode: int, cmd, output=None, stderr=None) -> None:
        super().__init__(f"command {cmd!r} returned non-zero exit {returncode}")
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr


class TimeoutExpired(Exception):
    def __init__(self, cmd, timeout) -> None:
        super().__init__(f"command {cmd!r} timed out after {timeout} seconds")
        self.cmd = cmd
        self.timeout = timeout


class CompletedProcess:
    def __init__(self, args, returncode: int, stdout=None, stderr=None) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def check_returncode(self) -> None:
        if self.returncode != 0:
            raise CalledProcessError(
                self.returncode,
                self.args,
                self.stdout,
                self.stderr,
            )


PIPE = -1
STDOUT = -2
DEVNULL = -3


def run(
    args,
    *,
    check=False,
    capture_output=False,
    text=False,
    input=None,
    stdout=None,
    stderr=None,
    timeout=None,
    encoding=None,
    env=None,
    cwd=None,
    **kwargs,
) -> CompletedProcess:
    if input is not None or stdout is not None or stderr is not None:
        raise NotImplementedError("subprocess stream redirection is not implemented")
    if cwd is not None or kwargs:
        raise NotImplementedError("subprocess advanced options are not implemented")
    if env is not None and timeout is not None:
        raise NotImplementedError("subprocess env= with timeout= is not implemented")
    capture = 1 if capture_output else 0
    if env is not None:
        # CPython replaces the environment rather than extending it.
        rc = py_subprocess_run_env(args, capture, env)
    elif timeout is None:
        rc = py_subprocess_run(args, capture)
    else:
        timeout_ms = int(timeout) * 1000
        if timeout_ms <= 0:
            raise ValueError("timeout must be positive")
        rc = py_subprocess_run_timeout(args, capture, timeout_ms)
        if rc == _TIMEOUT_RETURN_CODE:
            raise TimeoutExpired(args, timeout)
    result = CompletedProcess(args, rc, None, None)
    if check:
        result.check_returncode()
    return result


def check_output(args, **kwargs) -> bytes:
    if kwargs:
        raise NotImplementedError(
            "subprocess.check_output keyword options are not implemented"
        )
    return py_subprocess_check_output(args)


def check_call(args, **kwargs) -> int:
    result = run(args, check=True, **kwargs)
    return result.returncode


class Popen:
    def __init__(self, args, **kwargs) -> None:
        if kwargs:
            raise NotImplementedError(
                "subprocess.Popen keyword options are not implemented"
            )
        self.args = args
        self.returncode = py_subprocess_run(args, 0)

    def wait(self, timeout=None):
        if timeout is not None:
            raise NotImplementedError(
                "subprocess.Popen.wait timeout is not implemented"
            )
        return self.returncode

    def communicate(self, input=None, timeout=None):
        if input is not None or timeout is not None:
            raise NotImplementedError(
                "subprocess.Popen.communicate options are not implemented"
            )
        return (None, None)

    def poll(self):
        return self.returncode
