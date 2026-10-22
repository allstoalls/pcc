"""pcc.stdlib.subprocess — narrow ``subprocess`` skeleton.

Scope: ``run`` / ``check_output`` / ``check_call`` / ``Popen`` shape.
Implementation relies on extern ``posix_spawn`` + ``waitpid`` + pipe
FD plumbing; the interpreted fallback here uses CPython's real
subprocess so development proceeds even before the extern bindings
land.
"""

from __future__ import annotations

from pcc.extern import extern, c_int, c_int64, c_ptr, c_obj, c_rawptr, c_void

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
# Redirected streams: start returns a malloc-owned handle (see
# py_process_substrate); read returns NEW bytes or None per stream.
py_subprocess_capture_start = extern(
    "py_subprocess_capture_start",
    (c_obj, c_obj, c_obj, c_int64, c_int64),
    c_rawptr,
)
py_subprocess_capture_pid = extern("py_subprocess_capture_pid", (c_rawptr,), c_int64)
py_subprocess_capture_wait = extern(
    "py_subprocess_capture_wait", (c_rawptr, c_int64), c_int64
)
py_subprocess_capture_read = extern(
    "py_subprocess_capture_read", (c_rawptr, c_int64), c_obj
)
py_subprocess_capture_close = extern("py_subprocess_capture_close", (c_rawptr,), c_void)

_TIMEOUT_RETURN_CODE = -124


class CalledProcessError(Exception):
    def __init__(self, returncode: int, cmd, output=None, stderr=None) -> None:
        super().__init__(returncode, cmd, output, stderr)
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
        self.stderr = stderr

    def __str__(self) -> str:
        if self.returncode < 0:
            return f"Command '{self.cmd}' died with signal {-self.returncode}."
        return f"Command '{self.cmd}' returned non-zero exit status {self.returncode}."

    @property
    def stdout(self):
        # Alias for output, as in CPython.
        return self.output

    @stdout.setter
    def stdout(self, value):
        self.output = value


class TimeoutExpired(Exception):
    def __init__(self, cmd, timeout, output=None, stderr=None) -> None:
        super().__init__(cmd, timeout, output, stderr)
        self.cmd = cmd
        self.timeout = timeout
        self.output = output
        self.stderr = stderr

    def __str__(self) -> str:
        return f"Command '{self.cmd}' timed out after {self.timeout} seconds"

    @property
    def stdout(self):
        return self.output

    @stdout.setter
    def stdout(self, value):
        self.output = value


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


def _stream_spec(value, name) -> int:
    if value is None:
        return -1
    if value == PIPE:
        return -2
    if value == DEVNULL:
        return -3
    if value == STDOUT:
        if name == "stderr":
            return -4
        raise ValueError("STDOUT can only be used for stderr")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("invalid " + name + " file descriptor")
        return value
    return value.fileno()


def _translate_output(data, encoding, errors):
    # Popen.communicate's text mode: decode, then universal newlines.
    if data is None:
        return None
    text = data.decode(encoding or "utf-8", errors or "strict")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _program_name(args):
    if isinstance(args, str):
        return args
    return str(args[0])


def _run_redirected(args, check, stdout, stderr, timeout, text, encoding,
                    errors, env, cwd) -> CompletedProcess:
    out_spec = _stream_spec(stdout, "stdout")
    err_spec = _stream_spec(stderr, "stderr")
    timeout_ms = 0
    if timeout is not None:
        timeout_ms = int(timeout * 1000)
        if timeout_ms <= 0:
            timeout_ms = 1
    if cwd is not None:
        cwd = str(cwd)
    handle = py_subprocess_capture_start(args, env, cwd, out_spec, err_spec)
    pid = py_subprocess_capture_pid(handle)
    if pid < 0:
        py_subprocess_capture_close(handle)
        if pid == -2:
            raise FileNotFoundError(2, "No such file or directory", _program_name(args))
        raise OSError(-pid, "subprocess spawn failed")
    try:
        returncode = py_subprocess_capture_wait(handle, timeout_ms)
        output = py_subprocess_capture_read(handle, 1)
        error_output = py_subprocess_capture_read(handle, 2)
    finally:
        py_subprocess_capture_close(handle)
    if text:
        output = _translate_output(output, encoding, errors)
        error_output = _translate_output(error_output, encoding, errors)
    if returncode == -1000:
        raise TimeoutExpired(args, timeout, output, error_output)
    result = CompletedProcess(args, returncode, output, error_output)
    if check:
        result.check_returncode()
    return result


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
    errors=None,
    universal_newlines=None,
    env=None,
    cwd=None,
    **kwargs,
) -> CompletedProcess:
    if input is not None:
        raise NotImplementedError("subprocess input= is not implemented")
    if kwargs:
        raise NotImplementedError("subprocess advanced options are not implemented")
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError(
                "stdout and stderr arguments may not be used with capture_output."
            )
        stdout = PIPE
        stderr = PIPE
    text_mode = (
        bool(text) or bool(universal_newlines)
        or encoding is not None or errors is not None
    )
    if (stdout is not None or stderr is not None or cwd is not None
            or (env is not None and timeout is not None)):
        return _run_redirected(args, check, stdout, stderr, timeout, text_mode,
                               encoding, errors, env, cwd)
    capture = 0
    if env is not None:
        # CPython replaces the environment rather than extending it.
        rc = py_subprocess_run_env(args, capture, env)
    elif timeout is None:
        rc = py_subprocess_run(args, capture)
    else:
        timeout_ms = int(timeout * 1000)
        if timeout_ms <= 0:
            timeout_ms = 1
        rc = py_subprocess_run_timeout(args, capture, timeout_ms)
        if rc == _TIMEOUT_RETURN_CODE:
            raise TimeoutExpired(args, timeout)
    result = CompletedProcess(args, rc, None, None)
    if check:
        result.check_returncode()
    return result


def check_output(args, **kwargs):
    if kwargs:
        return run(args, stdout=PIPE, check=True, **kwargs).stdout
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
