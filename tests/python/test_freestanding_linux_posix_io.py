"""Linux C I/O/process leaves: model, owned object ABI, and admitted C execution.

Model/object cases do not link or execute an image. Native cases require the
explicit runtime fixture, including the changed libc member; no system libc.
"""
from __future__ import annotations

import ast
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pcc.backend.elf_x86_64 import parse_relocatable
from pcc.backend.owned_object_emit import emit_owned_object
from tests.owned_runtime_c_fixture import link_c_harness, runtime_ir

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/runtime/py/freestanding_linux_libc.py"
TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
SIGNATURES = {
    "read": ("i64", ("i32", "ptr", "i64")),
    "write": ("i64", ("i32", "ptr", "i64")),
    "socket": ("i32", ("i32", "i32", "i32")),
    "connect": ("i32", ("i32", "ptr", "i32")),
    "send": ("i64", ("i32", "ptr", "i64", "i32")),
    "recv": ("i64", ("i32", "ptr", "i64", "i32")),
    "pipe": ("i32", ("ptr",)),
    "fork": ("i32", ()),
    "waitpid": ("i32", ("i32", "ptr", "i32")),
}


def _model(machine, raw):
    """Execute actual leaf bodies with a recording kernel boundary, no syscalls."""
    calls, errors = [], []
    namespace = {"i64": int, "c_ptr": object, "pcc_errno_set": errors.append,
                 "target_platform_machine": lambda: machine,
                 "load_i8": lambda value, offset: ord(value[offset])}
    def kernel(name):
        def call(*args):
            calls.append((name, args))
            return raw
        return call
    for name in ("read", "write", "socket_open", "socket_connect", "socket_send",
                 "socket_recv", "waitpid", "syscall6"):
        namespace[name] = kernel(name)
    tree = ast.parse(SOURCE.read_text())
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {name + "_c" for name in SIGNATURES}]
    for node in tree.body:
        node.decorator_list = []
    exec(compile(tree, str(SOURCE), "exec"), namespace)
    return SimpleNamespace(**namespace), calls, errors


LEAVES = (
    ("read", (-1, 1234, 1), "read"),
    ("write", (-1, 1234, 1), "write"),
    ("socket", (2, 1 | 2048 | 524288, 0), "socket_open"),
    ("connect", (-1, 1234, 16), "socket_connect"),
    ("send", (-1, 1234, 1, 16384 | 64), "socket_send"),
    ("recv", (-1, 1234, 1, 2 | 64), "socket_recv"),
    ("waitpid", (-7, 1234, 1 | 2), "waitpid"),
)


@pytest.mark.parametrize("machine", ("x86_64", "aarch64"))
@pytest.mark.parametrize("name,args,operation", LEAVES)
@pytest.mark.parametrize("raw", (-4, -9, -11, -32, -88, -115, 0, 1, 4097))
def test_linux_posix_leaves_preserve_results_arguments_and_errno(machine, name, args, operation, raw):
    owner, calls, errors = _model(machine, raw)
    assert getattr(owner, name + "_c")(*args) == (-1 if raw < 0 else raw)
    assert calls == [(operation, args)]
    assert errors == ([-raw] if raw < 0 else [])


@pytest.mark.parametrize("name,operation", (("read", "read"), ("write", "write"),
                                           ("send", "socket_send"), ("recv", "socket_recv")))
@pytest.mark.parametrize("size", (0, (1 << 32) + 7, (1 << 64) - 1))
def test_linux_posix_io_keeps_size_t_bits(name, operation, size):
    owner, calls, errors = _model("x86_64", 0)
    args = (3, 1234, size) + ((0,) if name in ("send", "recv") else ())
    assert getattr(owner, name + "_c")(*args) == 0
    assert calls == [(operation, args)]
    assert errors == []


def test_linux_connect_normalizes_unsigned_socklen():
    owner, calls, _ = _model("x86_64", -22)
    assert owner.connect_c(3, 1234, -1) == -1
    assert calls == [("socket_connect", (3, 1234, 4294967295))]


@pytest.mark.parametrize("machine,pipe_nr,fork_args", (
    ("x86_64", 293, (57, 0, 0, 0, 0, 0, 0)),
    ("aarch64", 59, (220, 17, 0, 0, 0, 0, 0)),
))
@pytest.mark.parametrize("raw", (-4, -11, -12, -14, -24, 0, 12345))
def test_linux_pipe_and_fork_exact_kernel_contract(machine, pipe_nr, fork_args, raw):
    owner, calls, errors = _model(machine, raw)
    assert owner.pipe_c(1234) == (-1 if raw < 0 else raw)
    assert calls == [("syscall6", (pipe_nr, 1234, 0, 0, 0, 0, 0))]
    assert errors == ([-raw] if raw < 0 else [])
    owner, calls, errors = _model(machine, raw)
    assert owner.fork_c() == (-1 if raw < 0 else raw)
    assert calls == [("syscall6", fork_args)]
    assert errors == ([-raw] if raw < 0 else [])


@pytest.mark.parametrize("target", TARGETS)
def test_linux_posix_exports_have_owned_cross_target_abi(tmp_path, target):
    output = tmp_path / "freestanding_linux_libc.ll"
    # No subprocess, host cc, linker or runtime archive is allowed here.
    def forbidden(*args, **kwargs):
        raise AssertionError("owned object check attempted an external process")
    with patch("subprocess.Popen", forbidden):
        runtime_ir(SOURCE, output, target)
        text = output.read_text()
        data = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(data)
    obj = parse_relocatable(data)
    defined = {symbol.name for symbol in obj.symbols if symbol.section_index != 0}
    undefined = {symbol.name for symbol in obj.symbols if symbol.section_index == 0 and symbol.name}
    assert SIGNATURES.keys() <= defined
    assert undefined == {"pcc_errno_set", "pcc_platform_abort", "malloc", "free", "fflush",
                         "__fini_array_start", "__fini_array_end"}
    for name, (restype, arguments) in SIGNATURES.items():
        match = re.search(r"define external " + restype + r" @" + name + r"\(([^)]*)\)", text)
        assert match, name
        actual = tuple(part.strip().split()[0] for part in match[1].split(",") if part.strip())
        assert actual == arguments, (name, actual, arguments)
    # Verify lowering picked kernel operations, not undefined C self-calls.
    expected = (63, 64, 198, 203, 206, 207, 260) if target.startswith("aarch64") else (0, 1, 41, 42, 44, 45, 61)
    for name, number in zip(("read", "write", "socket", "connect", "send", "recv", "waitpid"), expected):
        body = text.split(" @" + name + "(", 1)[1].split("\n}", 1)[0]
        assert "i64 " + str(number) + "," in body, (name, number)


@pytest.mark.parametrize("target", ("i386-unknown-linux-gnu", "x86_64-apple-darwin"))
def test_linux_raw_process_leaf_rejects_unsupported_targets(tmp_path, target):
    source = tmp_path / "process_leaf.py"
    tree = ast.parse(SOURCE.read_text())
    fork = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "fork_c")
    source.write_text("from pcc import i64\nfrom pcc.extern import c_abi_typed_export, c_int32, c_void, extern\n"
                      "from pcc.unsafe import syscall6, target_platform_machine, load_i8\n"
                      "__pcc_freestanding__ = True\n"
                      'pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)\n' + ast.unparse(fork) + "\n")
    with pytest.raises((NotImplementedError, ValueError), match="syscall6 requires Linux x86_64 or AArch64|unsupported.*target|unsupported.*architecture"):
        runtime_ir(source, tmp_path / "unsupported.ll", target)


_NATIVE_C = r'''
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

/* Linux x86-64/AArch64 UAPI flags, independent of incomplete BSD headers. */
enum { LINUX_MSG_PEEK = 2, LINUX_MSG_DONTWAIT = 64, LINUX_MSG_NOSIGNAL = 16384, LINUX_SIGPIPE = 13 };
int64_t pcc_platform_tcp_listen(const char *, const char *, int64_t);
int64_t pcc_platform_socket_sockname(int64_t, void *, int64_t);
int64_t pcc_platform_tcp_accept(int64_t);

static int fail(int line) { return line; }
#define CHECK(c) do { if (!(c)) return fail(__LINE__); } while (0)

int main(void) {
    char buffer[16] = {0};
    int fd[2] = {-7, -8};
    errno = 123;
    CHECK(read(-1, buffer, 1) == -1 && errno == EBADF);
    CHECK(write(-1, buffer, 1) == -1 && errno == EBADF);
    CHECK(recv(-1, buffer, 1, 0) == -1 && errno == EBADF);
    CHECK(send(-1, buffer, 1, 0) == -1 && errno == EBADF);
    CHECK(connect(-1, (struct sockaddr *)buffer, 16) == -1 && errno == EBADF);
    CHECK(pipe((int *)0) == -1 && errno == EFAULT);
    errno = 123;
    CHECK(pipe(fd) == 0 && errno == 123);
    CHECK((fcntl(fd[0], F_GETFL) & O_NONBLOCK) == 0);
    CHECK((fcntl(fd[0], F_GETFD) & FD_CLOEXEC) == 0);
    CHECK(recv(fd[0], buffer, 1, 0) == -1 && errno == ENOTSOCK);
    CHECK(send(fd[1], buffer, 1, 0) == -1 && errno == ENOTSOCK);
    CHECK(fcntl(fd[0], F_SETFL, O_NONBLOCK) == 0);
    CHECK(read(fd[0], buffer, 1) == -1 && errno == EAGAIN);
    errno = 123;
    CHECK(write(fd[1], "abc", 3) == 3 && errno == 123);
    CHECK(read(fd[0], buffer, sizeof(buffer)) == 3 && errno == 123);
    CHECK(memcmp(buffer, "abc", 3) == 0);
    CHECK(read(fd[0], buffer, 0) == 0 && errno == 123);
    CHECK(write(fd[1], buffer, 0) == 0 && errno == 123);
    CHECK(close(fd[1]) == 0);
    CHECK(read(fd[0], buffer, sizeof(buffer)) == 0 && errno == 123);
    CHECK(close(fd[0]) == 0);

    /* A nonblocking write larger than an empty pipe must return a short
       count; the wrapper must not loop until EAGAIN or discard progress. */
    CHECK(pipe(fd) == 0);
    int capacity = fcntl(fd[1], 1032 /* Linux F_GETPIPE_SZ */);
    CHECK(capacity >= 4096);
    char *large = malloc((size_t)capacity + 1);
    CHECK(large != 0);
    memset(large, 0, (size_t)capacity + 1);
    CHECK(fcntl(fd[1], F_SETFL, O_NONBLOCK) == 0);
    errno = 123;
    CHECK(write(fd[1], large, (size_t)capacity + 1) == capacity && errno == 123);
    CHECK(write(fd[1], large, 1) == -1 && errno == EAGAIN);
    CHECK(read(fd[0], large, (size_t)capacity + 1) == capacity);
    free(large);
    close(fd[0]);
    close(fd[1]);

    CHECK(pipe(fd) == 0);
    errno = 123;
    pid_t child = fork();
    CHECK(child >= 0 && errno == 123);
    if (child == 0) {
        close(fd[1]);
        if (read(fd[0], buffer, 1) != 1) _exit(91);
        _exit(37);
    }
    close(fd[0]);
    int status = 0x1234;
    CHECK(waitpid(child, &status, WNOHANG) == 0 && status == 0x1234 && errno == 123);
    CHECK(write(fd[1], "x", 1) == 1);
    close(fd[1]);
    CHECK(waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) == 37);
    CHECK(waitpid(child, &status, 0) == -1 && errno == ECHILD);

    /* SIGPIPE's default disposition must survive the C write wrapper. */
    CHECK(pipe(fd) == 0);
    close(fd[0]);
    child = fork();
    CHECK(child >= 0);
    if (child == 0) { write(fd[1], "x", 1); _exit(92); }
    close(fd[1]);
    CHECK(waitpid(-1, &status, 0) == child);
    CHECK(WIFSIGNALED(status) && WTERMSIG(status) == LINUX_SIGPIPE);

    int64_t listener = pcc_platform_tcp_listen("127.0.0.1", "0", 0);
    CHECK(listener >= 0);
    unsigned char address[128] = {0};
    int64_t length = pcc_platform_socket_sockname(listener, address, sizeof(address));
    CHECK(length > 0);
    errno = 123;
    int client = socket(AF_INET, SOCK_STREAM, 0);
    CHECK(client >= 0 && errno == 123);
    CHECK(connect(client, (struct sockaddr *)0, 16) == -1 && errno == EFAULT);
    errno = 123;
    CHECK(connect(client, (struct sockaddr *)address, (socklen_t)length) == 0 && errno == 123);
    int peer = (int)pcc_platform_tcp_accept(listener);
    CHECK(peer >= 0);
    CHECK(fcntl(peer, F_SETFL, 0) == 0);
    CHECK(recv(client, buffer, 1, LINUX_MSG_DONTWAIT) == -1 && errno == EAGAIN);
    errno = 123;
    CHECK(send(client, "abc", 3, LINUX_MSG_NOSIGNAL) == 3 && errno == 123);
    CHECK(recv(peer, buffer, sizeof(buffer), LINUX_MSG_PEEK) == 3 && errno == 123);
    CHECK(recv(peer, buffer, sizeof(buffer), 0) == 3 && errno == 123);
    CHECK(memcmp(buffer, "abc", 3) == 0);
    CHECK(close(client) == 0);
    CHECK(recv(peer, buffer, sizeof(buffer), 0) == 0 && errno == 123);
    close(peer);
    close(listener);
    return 0;
}
'''


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64", "aarch64", "arm64"))
def test_linux_posix_exports_execute_original_c_abi_and_errno(tmp_path):
    source = tmp_path / "linux_posix_io.c"
    source.write_text(_NATIVE_C)
    executable = tmp_path / "linux_posix_io"
    link_c_harness(source, executable)
    result = subprocess.run([str(executable)], capture_output=True, timeout=20)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == result.stderr == b""


_EINTR_C = r'''
#include <errno.h>
#include <signal.h>
#include <unistd.h>
static void received(int signal_number) { (void)signal_number; }
int main(void) {
    struct sigaction action = {0};
    action.sa_handler = received;
    if (sigemptyset(&action.sa_mask) != 0) return 1;
    if (sigaction(10 /* Linux SIGUSR1 */, &action, 0) != 0) return 2;
    int descriptors[2];
    if (pipe(descriptors) != 0) return 3;
    if (write(1, "R", 1) != 1) return 4;
    char byte;
    if (read(descriptors[0], &byte, 1) != -1 || errno != EINTR) return 5;
    return 0;
}
'''


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64", "aarch64", "arm64"))
def test_linux_posix_read_reports_eintr_without_wrapper_retry(tmp_path):
    source = tmp_path / "linux_posix_eintr.c"
    source.write_text(_EINTR_C)
    executable = tmp_path / "linux_posix_eintr"
    link_c_harness(source, executable)
    with subprocess.Popen([str(executable)], stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            import selectors
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=10), "no signal-handler readiness marker"
                assert process.stdout.read(1) == b"R"
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    os.kill(process.pid, signal.SIGUSR1)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            output, error = process.communicate(timeout=2)
            assert process.returncode == 0, (process.returncode, output, error)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


def _c_object(source_text, target):
    from pcc.driver.project import TranslationUnit
    from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
    evaluator = CEvaluator(backend="self", target_triple=target)
    def forbidden(*args, **kwargs):
        raise AssertionError("C object check attempted an external process")
    with patch("subprocess.Popen", forbidden):
        units = evaluator.compile_translation_units(
            [TranslationUnit("posix_io.c", "posix_io.c", source_text)],
            use_system_cpp=False, use_compile_cache=False,
            include_dirs=[str(ROOT / "utils/fake_libc_include")],
        )
        return emit_owned_object(units[0][1], target)


@pytest.mark.parametrize("target,again,deadlock,progress,already,notsock", (
    ("x86_64-unknown-linux-gnu", 11, 35, 115, 114, 88),
    ("aarch64-unknown-linux-gnu", 11, 35, 115, 114, 88),
    ("arm64-apple-darwin", 35, 11, 36, 37, 38),
))
def test_posix_headers_select_target_errno_and_pid_declarations(target, again, deadlock, progress, already, notsock):
    source = """
#include <errno.h>
#include <sys/wait.h>
#include <unistd.h>
_Static_assert(sizeof(pid_t) == 4, "pid_t ABI");
_Static_assert(sizeof(ssize_t) == 8, "ssize_t ABI");
_Static_assert(sizeof(size_t) == 8, "size_t ABI");
_Static_assert(EBADF == 9 && ECHILD == 10, "descriptor and process errno");
_Static_assert(WNOHANG == 1 && WUNTRACED == 2, "wait options");
_Static_assert(EAGAIN == %d && EWOULDBLOCK == %d, "would block errno");
_Static_assert(EDEADLK == %d && EINPROGRESS == %d, "target errno");
_Static_assert(EALREADY == %d && ENOTSOCK == %d, "target socket errno");
pid_t call_process(int *fds, int *status) {
    if (pipe(fds) != 0) return -1;
    pid_t child = fork();
    return waitpid(child, status, WNOHANG);
}
""" % (again, again, deadlock, progress, already, notsock)
    assert _c_object(source, target)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", (_NATIVE_C, _EINTR_C), ids=("io-process", "eintr"))
def test_linux_posix_native_controls_compile_to_owned_objects(target, source):
    assert _c_object(source, target)
