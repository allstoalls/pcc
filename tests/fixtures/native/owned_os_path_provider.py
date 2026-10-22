"""One unchanged provider witness for CPython and owned native execution.

Calls through returned module/path values deliberately exercise ordinary
provider callables, including live replacement and saved-callable authority.
The separate original worker_resource_admission.py remains byte-identical.
"""

import gc
import os
import sys
import time


def system():
    return os


def paths():
    return os.path


class TextPath:
    def __init__(self, text, events, label):
        self.text = text
        self.events = events
        self.label = label

    def __fspath__(self):
        self.events.append(self.label)
        gc.collect()
        return self.text

    def __str__(self):
        raise AssertionError("a path must use __fspath__, not __str__")


class Index:
    def __init__(self, value, events, label):
        self.value = value
        self.events = events
        self.label = label

    def __index__(self):
        self.events.append(self.label)
        gc.collect()
        return self.value


def require_error(kind, function, *args):
    try:
        function(*args)
    except kind:
        return
    raise AssertionError("expected error: " + kind.__name__)


def main(root):
    events = []
    saved_path = paths()
    saved_abspath = saved_path.abspath
    saved_getcwd = system().getcwd
    cwd = saved_getcwd()
    assert saved_abspath(".") == cwd
    assert saved_abspath(sys.argv[0])
    assert sys.implementation.name in ("cpython", "pcc")
    moment = time.monotonic()
    time.sleep(0)
    assert time.monotonic() >= moment
    assert system().cpu_count() is None or system().cpu_count() >= 1
    assert len(system().urandom(0)) == 0
    assert len(system().urandom(8)) == 8
    if os.name != "nt":
        info = system().uname()
        assert len(info) == 5
        assert info.sysname == info[0] and info.machine == info[4]
        assert info.nodename == info[1]
        assert info.release == info[2] and info.version == info[3]

    assert saved_path.sep == os.sep
    assert saved_path.altsep == os.altsep
    assert saved_path.pathsep == os.pathsep
    assert saved_path.curdir == "." and saved_path.pardir == ".."
    assert saved_path.extsep == "." and saved_path.devnull == os.devnull
    work = saved_path.join(root, "work")
    nested = saved_path.join(work, "nested")
    system().makedirs(TextPath(nested, events, "mkdir"))
    # CPython's recursive wrapper may reconvert the final PathLike; the
    # existing owned helper consumes one converted text path. Check effects,
    # without claiming callback-count parity for that legacy helper.
    assert events and all(event == "mkdir" for event in events)
    system().makedirs(nested, exist_ok=True)
    require_error(OSError, system().makedirs, nested)
    assert saved_path.isdir(work) and not saved_path.isfile(work)
    assert not saved_path.islink(work)
    assert not saved_path.islink(work.encode())
    assert sorted(system().listdir()) == sorted(system().listdir(cwd))
    assert sorted(system().listdir(None)) == sorted(system().listdir(cwd))
    assert system().listdir(work) == ["nested"]

    events.clear()
    source = saved_path.join(work, "source.txt")
    assert saved_abspath(TextPath(source, events, "abspath")) == source
    assert events == ["abspath"]
    assert saved_path.basename(source) == "source.txt"
    assert saved_path.dirname(source) == work
    events.clear()
    assert saved_path.split(TextPath(source, events, "split")) == (work, "source.txt")
    assert events == ["split"]
    assert saved_path.splitext(source) == (saved_path.join(work, "source"), ".txt")
    assert saved_path.normpath(saved_path.join(work, ".", "nested", "..", "source.txt")) == source
    assert saved_path.normcase(source) == (source.lower() if os.name == "nt" else source)
    drive, tail = saved_path.splitdrive(source)
    assert drive + tail == source
    assert saved_path.isabs(source)
    assert not saved_path.isabs("source.txt")
    assert saved_path.relpath(source, work) == "source.txt"
    assert saved_path.relpath(cwd) == "."
    assert saved_path.commonpath([source, nested]) == work
    assert saved_path.commonprefix(["alpha", "alpine"]) == "alp"
    assert saved_path.commonprefix([]) == ""
    assert saved_path.expanduser(source) == source
    assert saved_path.expandvars(source) == source
    assert saved_path.abspath(saved_path.join(work, ".", "source.txt")) == source
    require_error(ValueError, saved_path.commonpath, [])
    require_error(ValueError, saved_path.relpath, "")
    require_error(TypeError, saved_abspath, object())

    with open(source, "w", encoding="utf-8") as stream:
        stream.write("provider witness\n")
    assert saved_path.exists(source) and saved_path.isfile(source)
    assert not saved_path.isdir(source)
    assert saved_path.getsize(source) == 17
    assert saved_path.getmtime(source) > 0
    assert saved_path.realpath(source) == source
    assert system().access(source, os.F_OK)
    system().chmod(source, 0o600)
    require_error(OSError, saved_path.getsize, source + ".missing")
    require_error(OSError, saved_path.getmtime, source + ".missing")
    assert not saved_path.isfile(source + "\0ignored")
    assert not saved_path.isdir(source + "\0ignored")
    assert not saved_path.islink(source + "\0ignored")

    events.clear()
    destination = saved_path.join(work, "replaced.txt")
    system().replace(TextPath(source, events, "source"),
                     TextPath(destination, events, "destination"))
    assert events == ["source", "destination"]
    assert not saved_path.exists(source) and saved_path.exists(destination)
    with open(destination, "r", encoding="utf-8") as stream:
        assert stream.read() == "provider witness\n"
    events.clear()
    assert system().kill(Index(system().getpid(), events, "pid"),
                         Index(0, events, "signal")) is None
    assert events == ["pid", "signal"]
    require_error(TypeError, system().kill, 1.5, 0)
    require_error(OverflowError, system().kill, 2 ** 100, 0)
    require_error(TypeError, system().kill, system().getpid(), 0.5)
    if os.name != "nt":
        require_error(ProcessLookupError, system().kill, 2147483647, 0)
    require_error(TypeError, system().urandom, 1.5)
    require_error(ValueError, system().urandom, -1)
    if os.name != "nt":
        system().rename(destination, source)
        destination = source

    with open(destination, "ab") as stream:
        assert system().write(stream.fileno(), b"!") == 1
    assert saved_path.getsize(destination) == 18
    if sys.implementation.name == "pcc":
        digest = "104786fed01c89a520af8f992942332036eb9c7c3fce3b78b881d708ed21f444"
        assert system()._pcc_sha256_file_hex(destination) == digest
        assert system()._pcc_sha256_file_hex_bounded(destination, 18) == digest
        assert system()._pcc_sha256_file_hex_bounded(destination, 17) == ""
    require_error(TypeError, system().write, 1.5, b"x")
    require_error(TypeError, system().write, 1, "x")
    key = "PCC_OS_PROVIDER_CONTROL_" + str(system().getpid())
    try:
        assert system().putenv(key, "provider-value") is None
    finally:
        assert system().unsetenv(key) is None

    events.clear()
    def receiver():
        events.append("receiver")
        gc.collect()
        return os.path
    def argument():
        events.append("argument")
        gc.collect()
        return TextPath(destination, events, "fspath")
    assert receiver().abspath(argument()) == destination
    assert events == ["receiver", "argument", "fspath"]

    class PathFailure:
        def __fspath__(self):
            raise LookupError("path callback failure")
    require_error(LookupError, saved_abspath, PathFailure())
    class InvalidPath:
        def __fspath__(self):
            return 42
    require_error(TypeError, saved_abspath, InvalidPath())

    def replacement(value):
        return "replaced:" + value
    saved_path.abspath = replacement
    try:
        assert paths().abspath("value") == "replaced:value"
        assert saved_abspath(destination) == destination
    finally:
        saved_path.abspath = saved_abspath
    class AlternatePath:
        def abspath(self, value):
            return "alternate:" + value
    os.path = AlternatePath()
    try:
        assert paths().abspath("value") == "alternate:value"
        assert saved_path.abspath(destination) == destination
    finally:
        os.path = saved_path
    def replacement_cwd():
        return "replacement cwd"
    os.getcwd = replacement_cwd
    try:
        assert system().getcwd() == "replacement cwd"
        assert saved_getcwd() == cwd
    finally:
        os.getcwd = saved_getcwd

    # The original resource driver needs this real atomic marker pattern.
    temporary = saved_path.join(work, "marker.tmp." + str(system().getpid()))
    marker = saved_path.join(work, "marker")
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write("complete")
    system().replace(temporary, marker)
    assert saved_path.exists(marker) and not saved_path.exists(temporary)
    system().unlink(marker)
    system().remove(destination)
    system().rmdir(nested)
    system().rmdir(work)
    assert not saved_path.exists(work)
    print("OWNED_OS_PATH_PROVIDER_OK")


def cwd_error(expected):
    """The runner supplies a private empty cwd which this process removes."""
    assert system().getcwd() == expected
    system().rmdir(expected)
    gc.collect()
    for operation in (system().getcwd, lambda: paths().abspath("."),
                      lambda: paths().relpath("child"), lambda: paths().realpath("child")):
        try:
            operation()
        except FileNotFoundError as error:
            assert error.errno == 2
            assert error.strerror
            assert error.args == (2, error.strerror)
        else:
            raise AssertionError("a removed current directory was accepted")
    print("OWNED_OS_CWD_ERROR_OK")


if __name__ == "__main__":
    if sys.argv[1] == "--cwd-error":
        cwd_error(sys.argv[2])
    else:
        main(sys.argv[1])
