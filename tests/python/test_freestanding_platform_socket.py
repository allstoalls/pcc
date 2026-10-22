from pathlib import Path
import platform
import subprocess
import sys

from pcc.frontends.python import pipeline


REPO_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_platform_socket.py"
)


def _compile_platform_ir(tmp_path: Path) -> Path:
    out = tmp_path / "freestanding_platform_socket.ll"
    pipeline.compile_python(
        str(PLATFORM_SOURCE),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return out


def _build_platform_object(tmp_path: Path, *, self_backend: bool) -> Path:
    llvm_ir = _compile_platform_ir(tmp_path)
    obj = tmp_path / ("platform_socket_self.o" if self_backend else "platform_socket.o")
    source = llvm_ir
    if self_backend:
        from pcc.backend.self_backend_dispatch import emit_self_asm

        source = tmp_path / "platform_socket.s"
        source.write_text(
            emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
        )
    build = subprocess.run(
        ["clang", "-c", str(source), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return obj


def _run_socket_harness(tmp_path: Path, name: str, obj: Path) -> None:
    harness = tmp_path / (name + ".c")
    executable = tmp_path / name
    harness.write_text(
        r'''
#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

int64_t pcc_platform_tcp_connect(const char *host, const char *port);
int64_t pcc_platform_tcp_listen(const char *host, const char *port, int64_t reuse_port);
int64_t pcc_platform_resolve_tcp(const char *host, const char *port, void *address);
int64_t pcc_platform_dns_connect_start(int64_t protocol, const char *host, const char *port, int64_t *out_fd);
int64_t pcc_platform_udp_connect_start(const char *host, const char *port, int64_t *out_fd);
int64_t pcc_platform_resolver_config_read(void *output, int64_t capacity);
int64_t pcc_platform_hosts_config_read(void *output, int64_t capacity);
int64_t pcc_platform_random_u16(void);
int64_t pcc_platform_socket_send(int64_t fd, const void *buf, int64_t n, int64_t flags);
int64_t pcc_platform_socket_recv(int64_t fd, void *buf, int64_t n, int64_t flags);
int64_t pcc_platform_tcp_accept(int64_t fd);
int64_t pcc_platform_socket_shutdown(int64_t fd, int64_t how);
int64_t pcc_platform_socket_sockname(int64_t fd, void *address, int64_t capacity);
int64_t pcc_platform_socket_peername(int64_t fd, void *address, int64_t capacity);
int64_t pcc_platform_poll_fd(int64_t fd, int64_t events, int64_t timeout_ms);
int64_t pcc_platform_poll_readable_pair(int64_t fd0, int64_t fd1, int64_t timeout_ms);

static int run_client_case(int listen_fd, const char *host, const char *port) {
    pid_t child = fork();
    if (child < 0) return 1;
    if (child == 0) {
        int64_t fd = pcc_platform_tcp_connect(host, port);
        if (fd < 0) _exit(11);
        if (pcc_platform_socket_send(fd, "ping", 4, 0) != 4) _exit(12);
        char reply[4] = {0};
        if (pcc_platform_poll_fd(fd, 1, 2000) <= 0) _exit(13);
        if (pcc_platform_socket_recv(fd, reply, 4, 0) != 4) _exit(13);
        close((int)fd);
        _exit(memcmp(reply, "pong", 4) == 0 ? 0 : 14);
    }
    int peer = (int)pcc_platform_tcp_accept(listen_fd);
    if (peer < 0) return 2;
    char request[4] = {0};
    if (pcc_platform_poll_fd(peer, 1, 2000) <= 0) return 3;
    if (recv(peer, request, 4, 0) != 4 || memcmp(request, "ping", 4) != 0) return 3;
    if (send(peer, "pong", 4, 0) != 4) return 4;
    close(peer);
    int status = 0;
    if (waitpid(child, &status, 0) != child) return 5;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 6;
}

int main(void) {
    char config_snapshot[65535];
    if (pcc_platform_resolver_config_read(config_snapshot, sizeof(config_snapshot)) <= 0) return 10;
    if (pcc_platform_hosts_config_read(config_snapshot, sizeof(config_snapshot)) <= 0) return 11;
    if (pcc_platform_random_u16() <= 0) return 17;

    int64_t dns_fd = -1;
    if (pcc_platform_dns_connect_start(0, "127.0.0.1", "9", &dns_fd) != 3) return 12;
    if (dns_fd < 0) return 13;
    int dns_flags = fcntl((int)dns_fd, F_GETFL, 0);
    if (dns_flags < 0 || (dns_flags & O_NONBLOCK) == 0) return 14;
    close((int)dns_fd);
    dns_fd = -1;
    if (pcc_platform_udp_connect_start("127.0.0.1", "9", &dns_fd) != 3) return 15;
    if (dns_fd < 0) return 16;
    close((int)dns_fd);

    struct sockaddr_in6 resolved6;
    memset(&resolved6, 0, sizeof(resolved6));
    if (pcc_platform_resolve_tcp("::1", "443", &resolved6) != sizeof(resolved6)) return 15;
    if (resolved6.sin6_family != AF_INET6 || ntohs(resolved6.sin6_port) != 443) return 16;
    if (resolved6.sin6_addr.s6_addr[15] != 1) return 17;

    int64_t owned_listener = pcc_platform_tcp_listen("127.0.0.1", "0", 0);
    if (owned_listener < 0) return 18;
    int owned_flags = fcntl((int)owned_listener, F_GETFL, 0);
    if (owned_flags < 0 || (owned_flags & O_NONBLOCK) == 0) return 19;
    unsigned char owned_name[128] = {0};
    if (pcc_platform_socket_sockname(owned_listener, owned_name, sizeof(owned_name)) <= 0) return 24;
    close((int)owned_listener);

    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair) != 0) return 25;
    if (pcc_platform_poll_fd(pair[0], 1, 0) != 0) return 26;
    if (write(pair[1], "x", 1) != 1) return 27;
    if (pcc_platform_poll_fd(pair[0], 1, 0) <= 0) return 28;
    if ((pcc_platform_poll_readable_pair(pair[0], pair[1], 0) & 1) == 0) return 29;
    if (pcc_platform_socket_shutdown(pair[1], 1) != 0) return 30;
    close(pair[0]);
    close(pair[1]);

    int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) return 20;
    int one = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) return 21;
    if (listen(listen_fd, 4) != 0) return 22;
    socklen_t addr_len = sizeof(addr);
    if (getsockname(listen_fd, (struct sockaddr *)&addr, &addr_len) != 0) return 23;
    char port[16];
    snprintf(port, sizeof(port), "%u", (unsigned)ntohs(addr.sin_port));

    int rc = run_client_case(listen_fd, "127.0.0.1", port);
    if (rc != 0) return 30 + rc;
    rc = run_client_case(listen_fd, "localhost", port);
    if (rc != 0) return 60 + rc;
    if (pcc_platform_tcp_connect("pcc-name-must-fail.invalid", port) >= 0) return 90;
    close(listen_fd);
    return 0;
}
''',
        encoding="utf-8",
    )
    link = subprocess.run(
        ["clang", str(harness), str(obj), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert link.returncode == 0, link.stdout + link.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_platform_socket_llvm_numeric_and_hosts_resolver(tmp_path):
    _run_socket_harness(
        tmp_path,
        "platform_socket_llvm",
        _build_platform_object(tmp_path, self_backend=False),
    )


def test_numeric_address_decimal_uses_raw_nonzero_division_intrinsics(
    tmp_path: Path,
) -> None:
    """Freestanding address formatting must not synthesize Python exceptions."""
    ir_text = _compile_platform_ir(tmp_path).read_text(encoding="utf-8")
    body = ir_text.split(
        "define i64 @pcc_platform_socket_append_address_decimal", 1
    )[1].split("\ndefine ", 1)[0]
    assert "call ptr @py_exc_new" not in body
    assert " udiv i64 " in body
    assert " urem i64 " in body


def test_platform_socket_self_numeric_and_hosts_resolver(tmp_path):
    _run_socket_harness(
        tmp_path,
        "platform_socket_self",
        _build_platform_object(tmp_path, self_backend=True),
    )


def test_platform_socket_has_only_named_darwin_boundary(tmp_path):
    obj = _build_platform_object(tmp_path, self_backend=False)
    undefined = subprocess.run(
        ["nm", "-u", str(obj)], capture_output=True, text=True, timeout=30
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    if sys.platform == "darwin":
        assert set(undefined.stdout.split()) == {
            "___error",
            "_accept",
            "_bind",
            "_close",
            "_connect",
            "_fcntl",
            "_listen",
            "_open",
            "_getpeername",
            "_getsockname",
            "_getsockopt",
            "_poll",
            "_read",
            "_recv",
            "_send",
            "_setsockopt",
            "_socket",
            "_shutdown",
        }
    else:
        assert sys.platform.startswith("linux") and platform.machine() == "x86_64"
        assert undefined.stdout.strip() == ""


def test_linux_platform_socket_uses_raw_syscalls(tmp_path, monkeypatch):
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_sys_platform_text", lambda self: "linux"
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin, "_target_machine_text", lambda self: "x86_64"
    )
    ir_text = _compile_platform_ir(tmp_path).read_text(encoding="utf-8")
    declarations = [line for line in ir_text.splitlines() if line.startswith("declare ")]
    for symbol in (
        "__error", "accept", "bind", "close", "connect", "fcntl", "listen", "open",
        "read", "recv", "send", "getsockopt", "setsockopt", "socket", "shutdown",
        "getsockname", "getpeername", "poll",
    ):
        assert all("@" + symbol + "(" not in line for line in declarations)
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    assembly = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert assembly.count("syscall") >= 8


def test_runtime_archive_routes_socket_consumers_to_python_owner(
    pcc_runtime_archive,
):
    undefined = subprocess.run(
        ["nm", "-A", "-u", str(pcc_runtime_archive)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert undefined.returncode == 0, undefined.stdout + undefined.stderr
    consumers = {
        name: [
            line
            for line in undefined.stdout.splitlines()
            if (":" + name + ":") in line
        ]
        for name in ("py_http_runtime.o", "py_asyncio_io_runtime.o")
    }
    forbidden = {"getaddrinfo", "freeaddrinfo", "socket", "connect", "send", "recv"}
    for name, lines in consumers.items():
        imported = {line.rstrip().split()[-1].lstrip("_") for line in lines}
        assert not (imported & forbidden), (name, sorted(imported & forbidden))
        assert "pcc_platform_socket_send" in imported, name
        assert "pcc_platform_socket_recv" in imported, name
        assert "pcc_platform_tcp_connect" in imported, name
    assert "pcc_platform_tcp_accept" in {
        line.rstrip().split()[-1].lstrip("_")
        for line in consumers["py_asyncio_io_runtime.o"]
    }

    plan = subprocess.run(
        ["make", "-B", "-n", "libpy_runtime_pcc_py.a"],
        cwd=REPO_ROOT / "pcc" / "runtime",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert plan.returncode == 0, plan.stdout + plan.stderr
    archive_line = next(
        line
        for line in plan.stdout.splitlines()
        if "ar rcs libpy_runtime_pcc_py.a.tmp" in line
    )
    assert "build_py/freestanding_platform_socket.o" in archive_line
