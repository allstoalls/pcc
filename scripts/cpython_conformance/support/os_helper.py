"""``test.support.os_helper`` for the conformance harness.

POSIX semantics with the UTF-8 file system encoding of macOS and Linux.
"""

import contextlib
import errno
import functools
import os
import shutil
import stat
import sys
import unittest

from test import support


TESTFN_ASCII = "@test"
TESTFN_ASCII = f"{TESTFN_ASCII}_{os.getpid()}_tmp"

if support.is_apple:
    # Apple file names are canonically decomposed: this is the NFD form of
    # the composed name below.
    TESTFN_UNICODE = TESTFN_ASCII + "-àòɘŁğ"
else:
    TESTFN_UNICODE = TESTFN_ASCII + "-\xe0\xf2ɘŁğ"


# The file names below are what CPython's helper computes for the UTF-8
# file system encoding (with surrogateescape) of macOS and Linux.
if support.is_apple:
    TESTFN_UNENCODABLE = None
else:
    TESTFN_UNENCODABLE = TESTFN_ASCII + "-" + chr(0xDCFF)

FS_NONASCII = "\u00E6"

SAVEDCWD = os.getcwd()

TESTFN_UNDECODABLE = TESTFN_ASCII.encode("ascii") + b"\xe7w\xf0"

if FS_NONASCII:
    TESTFN_NONASCII = TESTFN_ASCII + FS_NONASCII
else:
    TESTFN_NONASCII = None
TESTFN = TESTFN_NONASCII or TESTFN_ASCII


def make_bad_fd():
    """An invalid file descriptor: one opened and closed again."""
    file = open(TESTFN, "wb")
    try:
        return file.fileno()
    finally:
        file.close()
        unlink(TESTFN)


_can_symlink = None


def can_symlink():
    global _can_symlink
    if _can_symlink is not None:
        return _can_symlink
    src = os.path.abspath(TESTFN)
    symlink_path = src + "can_symlink"
    try:
        os.symlink(src, symlink_path)
        can = True
    except (OSError, NotImplementedError, AttributeError):
        can = False
    else:
        os.remove(symlink_path)
    _can_symlink = can
    return can


def skip_unless_symlink(test):
    ok = can_symlink()
    return test if ok else unittest.skip("Requires functional symlink implementation")(test)


def can_hardlink():
    return hasattr(os, "link")


def skip_unless_hardlink(test):
    return test if can_hardlink() else unittest.skip("requires hardlink support")(test)


def can_xattr():
    return False


def skip_unless_xattr(test):
    return unittest.skip("no non-broken extended attribute support")(test)


_can_chmod = None


def can_chmod():
    global _can_chmod
    if _can_chmod is not None:
        return _can_chmod
    if not hasattr(os, "chmod"):
        _can_chmod = False
        return _can_chmod
    try:
        with open(TESTFN, "wb"):
            try:
                os.chmod(TESTFN, 0o555)
                mode1 = os.stat(TESTFN).st_mode
                os.chmod(TESTFN, 0o777)
                mode2 = os.stat(TESTFN).st_mode
            except OSError:
                can = False
            else:
                can = stat.S_IMODE(mode1) != stat.S_IMODE(mode2)
    finally:
        unlink(TESTFN)
    _can_chmod = can
    return can


def skip_unless_working_chmod(test):
    return test if can_chmod() else unittest.skip("requires working os.chmod()")(test)


_can_dac_override = None


def can_dac_override():
    global _can_dac_override
    if not can_chmod():
        _can_dac_override = False
    if _can_dac_override is not None:
        return _can_dac_override
    try:
        with open(TESTFN, "wb"):
            os.chmod(TESTFN, 0o400)
            try:
                with open(TESTFN, "wb"):
                    pass
            except OSError:
                _can_dac_override = False
            else:
                _can_dac_override = True
    finally:
        try:
            os.chmod(TESTFN, 0o700)
        except OSError:
            pass
        unlink(TESTFN)
    return _can_dac_override


def skip_if_dac_override(test):
    ok = not can_dac_override()
    return test if ok else unittest.skip("incompatible with CAP_DAC_OVERRIDE")(test)


def skip_unless_dac_override(test):
    return test if can_dac_override() else unittest.skip("requires CAP_DAC_OVERRIDE")(test)


@contextlib.contextmanager
def save_mode(path, *, quiet=False):
    saved_mode = os.stat(path)
    try:
        yield
    finally:
        try:
            os.chmod(path, saved_mode.st_mode)
        except OSError:
            if not quiet:
                raise


def unlink(filename):
    try:
        os.unlink(filename)
    except (FileNotFoundError, NotADirectoryError):
        pass


def rmdir(dirname):
    try:
        os.rmdir(dirname)
    except FileNotFoundError:
        pass


def rmtree(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


@contextlib.contextmanager
def temp_dir(path=None, quiet=False):
    """A temporary directory: ``path`` created, or a new mkdtemp one."""
    import tempfile
    dir_created = False
    if path is None:
        path = tempfile.mkdtemp()
        dir_created = True
        path = os.path.realpath(path)
    else:
        try:
            os.mkdir(path)
            dir_created = True
        except OSError:
            if not quiet:
                raise
    if dir_created:
        pid = os.getpid()
    try:
        yield path
    finally:
        if dir_created and pid == os.getpid():
            rmtree(path)


@contextlib.contextmanager
def change_cwd(path, quiet=False):
    """Temporarily change the current working directory."""
    saved_dir = os.getcwd()
    try:
        os.chdir(os.path.realpath(path))
    except OSError:
        if not quiet:
            raise
    try:
        yield os.getcwd()
    finally:
        os.chdir(saved_dir)


@contextlib.contextmanager
def temp_cwd(name="tempcwd", quiet=False):
    """Temporarily create a directory and make it the working directory."""
    with temp_dir(path=name, quiet=quiet) as temp_path:
        with change_cwd(temp_path, quiet=quiet) as cwd_dir:
            yield cwd_dir


def create_empty_file(filename):
    """Create an empty file. If the file already exists, truncate it."""
    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    os.close(fd)


@contextlib.contextmanager
def open_dir_fd(path):
    """Open a file descriptor to a directory."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dir_fd = os.open(path, flags)
    try:
        yield dir_fd
    finally:
        os.close(dir_fd)


def fs_is_case_insensitive(directory):
    import tempfile
    with tempfile.NamedTemporaryFile(dir=directory) as base:
        base_path = base.name
        case_path = base_path.upper()
        if case_path == base_path:
            case_path = base_path.lower()
        try:
            return os.path.samefile(base_path, case_path)
        except FileNotFoundError:
            return False


class FakePath:
    """Simple implementation of the path protocol."""

    def __init__(self, path):
        self.path = path

    def __repr__(self):
        return f"<FakePath {self.path!r}>"

    def __fspath__(self):
        if (isinstance(self.path, BaseException) or
                isinstance(self.path, type) and issubclass(self.path, BaseException)):
            raise self.path
        return self.path


def fd_count():
    """Count the number of open file descriptors."""
    if sys.platform.startswith(("linux", "android", "freebsd")):
        fd_path = "/proc/self/fd"
    elif support.is_apple:
        fd_path = "/dev/fd"
    else:
        fd_path = None
    if fd_path is not None:
        try:
            names = os.listdir(fd_path)
            return len(names) - 1
        except FileNotFoundError:
            pass
    MAXFD = 256
    if hasattr(os, "sysconf"):
        try:
            MAXFD = os.sysconf("SC_OPEN_MAX")
        except OSError:
            pass
    count = 0
    for fd in range(MAXFD):
        try:
            fd2 = os.dup(fd)
        except OSError as e:
            if e.errno != errno.EBADF:
                raise
        else:
            os.close(fd2)
            count += 1
    return count


@contextlib.contextmanager
def temp_umask(umask):
    """Temporarily set the process umask."""
    oldmask = os.umask(umask)
    try:
        yield
    finally:
        os.umask(oldmask)


class EnvironmentVarGuard:
    """Restore every environment variable it changes on exit.

    CPython's helper derives its mapping methods from
    collections.abc.MutableMapping; they are spelled out here.
    """

    def __init__(self):
        self._environ = os.environ
        self._changed = {}

    def __getitem__(self, envvar):
        return self._environ[envvar]

    def __setitem__(self, envvar, value):
        if envvar not in self._changed:
            self._changed[envvar] = self._environ.get(envvar)
        self._environ[envvar] = value

    def __delitem__(self, envvar):
        if envvar not in self._changed:
            self._changed[envvar] = self._environ.get(envvar)
        if envvar in self._environ:
            del self._environ[envvar]

    def __contains__(self, envvar):
        return envvar in self._environ

    def __iter__(self):
        return iter(self._environ)

    def __len__(self):
        return len(self._environ)

    def keys(self):
        return self._environ.keys()

    def items(self):
        return self._environ.items()

    def values(self):
        return self._environ.values()

    def get(self, envvar, default=None):
        if envvar in self._environ:
            return self._environ[envvar]
        return default

    def pop(self, envvar, *default):
        if envvar in self._environ:
            value = self._environ[envvar]
            del self[envvar]
            return value
        if default:
            return default[0]
        raise KeyError(envvar)

    def setdefault(self, envvar, default=None):
        if envvar not in self._environ:
            self[envvar] = default
        return self[envvar]

    def update(self, other=(), /, **kwds):
        if hasattr(other, "keys"):
            for key in other.keys():
                self[key] = other[key]
        else:
            for key, value in other:
                self[key] = value
        for key, value in kwds.items():
            self[key] = value

    def set(self, envvar, value):
        self[envvar] = value

    def unset(self, envvar, /, *envvars):
        """Unset one or more environment variables."""
        for ev in (envvar, *envvars):
            del self[ev]

    def copy(self):
        return dict(self._environ)

    def __enter__(self):
        return self

    def __exit__(self, *ignore_exc):
        for (k, v) in self._changed.items():
            if v is None:
                if k in self._environ:
                    del self._environ[k]
            else:
                self._environ[k] = v
        os.environ = self._environ


def without_source_date_epoch(fxn):
    """Runs function with SOURCE_DATE_EPOCH unset."""
    @functools.wraps(fxn)
    def wrapper(*args, **kwargs):
        with EnvironmentVarGuard() as env:
            env.unset("SOURCE_DATE_EPOCH")
            return fxn(*args, **kwargs)
    return wrapper


def with_source_date_epoch(fxn=None, *, epoch=123456789):
    """Runs function with SOURCE_DATE_EPOCH set to *epoch*."""
    if fxn is None:
        return functools.partial(with_source_date_epoch, epoch=epoch)

    @functools.wraps(fxn)
    def wrapper(*args, **kwargs):
        with EnvironmentVarGuard() as env:
            env["SOURCE_DATE_EPOCH"] = str(epoch)
            return fxn(*args, **kwargs)
    return wrapper


def subst_drive(path):
    raise unittest.SkipTest("ctypes or kernel32 is not available")
