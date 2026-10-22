"""Level-1 pcc1 replacement gate for the frozen pproxy 1.9.5 service.

The pcc path installs the unmodified vendored source, then starts, exercises,
reloads and shuts it down with host Python/libpython disabled.  CPython runs as
a separate behavioral oracle and is never present in the pcc process tree.
The mandatory 30-minute resource report remains a release gate, not something
silently approximated by this bounded integration test.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

from tests.python.pcc1_gate import find_current_pcc1


ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "projects" / "python-proxy"
PINNED_TREE_SHA256 = "d91826af8d1979a3a31ab66edd7b386892aad875bece7f4e245573bedf9872c9"

pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(probe="pcc1")]


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in (".pyc", ".pyo")
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_listening(
    port: int,
    *,
    process: subprocess.Popen[str],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            raise AssertionError(
                f"service exited before listening on {port}:\n{stdout}\n{stderr}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"service did not listen on {port} within {timeout} seconds")


class _Origin:
    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self.port = int(self._socket.getsockname()[1])
        self._socket.listen(64)
        self._socket.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._socket.accept()
            except (OSError, TimeoutError):
                continue
            threading.Thread(
                target=self._reply,
                args=(conn,),
                daemon=True,
            ).start()

    @staticmethod
    def _reply(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                request.extend(chunk)
            first = bytes(request).split(b"\r\n", 1)[0]
            parts = first.split(b" ")
            path = parts[1] if len(parts) > 1 else b"/invalid"
            body = b"pcc-service-oracle:" + path
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                + b"Content-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )


def _read_exact(sock: socket.socket, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        chunk = sock.recv(size - len(out))
        if not chunk:
            raise EOFError(f"short socket read: wanted {size}, received {len(out)}")
        out.extend(chunk)
    return bytes(out)


def _read_http_response(sock: socket.socket) -> bytes:
    chunks = bytearray()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.extend(chunk)
    response = bytes(chunks)
    assert response.startswith(b"HTTP/1.1 200"), response
    assert b"\r\n\r\n" in response
    return response.split(b"\r\n\r\n", 1)[1]


def _request(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(request)
        return _read_http_response(sock)


def _proxy_get(proxy_port: int, origin_port: int, index: int) -> bytes:
    path = f"/item/{index}"
    return _request(
        proxy_port,
        (
            f"GET http://127.0.0.1:{origin_port}{path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{origin_port}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )


def _proxy_get_eventually(
    proxy_port: int,
    origin_port: int,
    index: int,
    *,
    timeout: float,
) -> bytes:
    """Wait through the deliberate close/rebind window of an admin reload."""
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return _proxy_get(proxy_port, origin_port, index)
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(
        f"proxy did not serve after reload within {timeout} seconds: {last_error}"
    )


def _socks_get(proxy_port: int, origin_port: int, index: int) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy_port), timeout=10) as sock:
        sock.settimeout(10)
        sock.sendall(b"\x05\x01\x00")
        assert _read_exact(sock, 2) == b"\x05\x00"
        sock.sendall(
            b"\x05\x01\x00\x01"
            + socket.inet_aton("127.0.0.1")
            + origin_port.to_bytes(2, "big")
        )
        reply = _read_exact(sock, 4)
        assert reply[:2] == b"\x05\x00", reply
        atyp = reply[3]
        if atyp == 1:
            _read_exact(sock, 4)
        elif atyp == 3:
            _read_exact(sock, _read_exact(sock, 1)[0])
        elif atyp == 4:
            _read_exact(sock, 16)
        else:
            raise AssertionError(f"invalid SOCKS5 reply address type: {atyp}")
        _read_exact(sock, 2)
        path = f"/socks/{index}"
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{origin_port}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
        )
        return _read_http_response(sock)


def _admin_request(port: int, method: str, path: str, body: bytes = b"") -> bytes:
    return _request(
        port,
        (
            f"{method} {path} HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        + body,
    )


def _descendants(root_pid: int) -> list[tuple[int, str, int]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    children: dict[int, list[tuple[int, str]]] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        pid, parent = int(fields[0]), int(fields[1])
        children.setdefault(parent, []).append((pid, fields[2]))
    pending = [(root_pid, 0)]
    out: list[tuple[int, str, int]] = []
    while pending:
        parent, depth = pending.pop()
        for item in children.get(parent, []):
            out.append((item[0], item[1], depth + 1))
            pending.append((item[0], depth + 1))
    return sorted(out, key=lambda item: (item[2], item[0]))


def _shutdown(
    process: subprocess.Popen[str], descendants: list[tuple[int, str, int]]
):
    # pcc1 waits for the compiled service child.  Signal that leaf so the
    # service exercises its own graceful asyncio drain and pcc1 can propagate
    # the exact exit status.  A compiler/restart chain may add more than one
    # process level, so the last observed descendant is deliberately the
    # deepest live leaf rather than the pcc1 wrapper.
    target = descendants[-1][0] if descendants else process.pid
    os.kill(target, signal.SIGINT)
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError("service did not drain after SIGINT:\n" + stdout + stderr)
    assert process.returncode == 0, stdout + stderr
    assert "Task was destroyed but it is pending" not in stdout + stderr
    return stdout, stderr


def _exercise_service(
    command_prefix: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    origin_port: int,
    startup_timeout: float,
) -> dict[str, object]:
    proxy_port = _free_port()
    admin_port = _free_port()
    service_args = [
        "-l",
        f"http+socks5://127.0.0.1:{proxy_port}",
        "-l",
        f"httpadmin://127.0.0.1:{admin_port}",
        "-r",
        "direct://",
    ]
    process = subprocess.Popen(
        [*command_prefix, *service_args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_listening(proxy_port, process=process, timeout=startup_timeout)
        _wait_listening(admin_port, process=process, timeout=30.0)
        descendants = _descendants(process.pid)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(_proxy_get, proxy_port, origin_port, index)
                for index in range(16)
            ]
            before_reload = [future.result(timeout=30) for future in futures]
        socks_before_reload = _socks_get(proxy_port, origin_port, 17)
        status = json.loads(_admin_request(admin_port, "GET", "/status"))
        reload_body = " ".join(service_args).encode("utf-8")
        reload_result = json.loads(
            _admin_request(admin_port, "POST", "/configs", reload_body)
        )
        after_reload = _proxy_get_eventually(
            proxy_port,
            origin_port,
            99,
            timeout=30.0,
        )
        socks_after_reload = _socks_get(proxy_port, origin_port, 117)
        stdout, stderr = _shutdown(process, descendants)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
    return {
        "before_reload": [body.decode("utf-8") for body in before_reload],
        "after_reload": after_reload.decode("utf-8"),
        "socks_before_reload": socks_before_reload.decode("utf-8"),
        "socks_after_reload": socks_after_reload.decode("utf-8"),
        "status": status,
        "reload": reload_result,
        "descendants": [command for _, command, _ in descendants],
        "stderr": stderr,
        "stdout": stdout,
    }


def _install_report(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    marker = '{"command": "install"'
    start = process.stdout.find(marker)
    assert start >= 0, process.stdout + process.stderr
    return json.loads(process.stdout[start:])


def _assert_no_libpython(path: Path) -> None:
    if sys.platform == "darwin":
        command = ["otool", "-L", str(path)]
    else:
        command = ["readelf", "-d", str(path)]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    lowered = result.stdout.lower()
    assert "libpython" not in lowered
    assert "python3" not in lowered


def test_current_pcc1_replaces_cpython_for_frozen_pproxy_service(
    tmp_path: Path,
    pcc_runtime_archive: Path,
):
    assert _tree_sha256(PROJECT) == PINNED_TREE_SHA256
    pcc1 = find_current_pcc1(ROOT)
    assert pcc1 is not None, "receipt-current pcc1 is required"

    oracle = os.environ.get("PCC_CPYTHON_3132_ORACLE", sys.executable)
    oracle_version = subprocess.run(
        [oracle, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert oracle_version.stdout.strip() == "3.15.0"

    pcc_env = os.environ.copy()
    for name in ("LC_ALL", "PYTHONPATH", "PCC_PACKAGE_SITE", "PCC_DATA_HOME"):
        pcc_env.pop(name, None)
    pcc_env.update(
        {
            "VIRTUAL_ENV": str(tmp_path / "venv"),
            "PCC_HOST_PYTHON": "/usr/bin/false",
            "PCC_HOST_PCC": "/usr/bin/false",
            "PCC_COMPAT_PYTHON": "/usr/bin/false",
            "PCC_RUNTIME_ARCHIVE": str(pcc_runtime_archive),
            "PCC_RUNTIME_CC": "pcc",
            "PCC_RUNTIME_HIGH": "py",
            "PCC_PY_RUN_CACHE_DIR": str(tmp_path / "run-cache"),
            "PCC_BOOTSTRAP_SUBPROCESS_TIMEOUT_SECONDS": "600",
        }
    )
    deny_bin = tmp_path / "deny-host-python"
    deny_bin.mkdir()
    for name in ("python", "python3"):
        denied = deny_bin / name
        denied.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        denied.chmod(0o755)
    pcc_env["PATH"] = str(deny_bin) + os.pathsep + pcc_env.get("PATH", "")
    install = subprocess.run(
        [str(pcc1), "-m", "pip", "install", "--build=owned", str(PROJECT)],
        env=pcc_env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    report = _install_report(install)
    assert report["ok"] is True
    assert report["build_mode_requested"] == "owned"
    assert len(report["installs"]) == 1
    installed = report["installs"][0]
    assert installed["ok"] is True
    assert installed["no_libpython_runtime"] is True
    build_report = installed["build_report"]
    assert build_report["build_ownership"] == "not-required"
    assert build_report["host_assisted"] is False
    assert build_report["host_python"] is None
    assert build_report["host_free_build_claim"] is True

    origin = _Origin()
    origin.start()
    try:
        oracle_env = os.environ.copy()
        oracle_env.pop("LC_ALL", None)
        oracle_env["PYTHONPATH"] = str(PROJECT)
        expected = _exercise_service(
            [oracle, "-m", "pproxy"],
            cwd=tmp_path,
            env=oracle_env,
            origin_port=origin.port,
            startup_timeout=30.0,
        )
        for backend in ("0", "1", "2", "3", "4"):
            run_env = dict(pcc_env)
            run_env["PCC_GC_BACKEND"] = backend
            actual = _exercise_service(
                [str(pcc1), "-m", "pproxy"],
                cwd=tmp_path,
                env=run_env,
                origin_port=origin.port,
                startup_timeout=600.0,
            )
            assert actual["before_reload"] == expected["before_reload"]
            assert actual["after_reload"] == expected["after_reload"]
            assert actual["socks_before_reload"] == expected["socks_before_reload"]
            assert actual["socks_after_reload"] == expected["socks_after_reload"]
            assert actual["status"] == expected["status"] == {"status": "ok"}
            assert actual["reload"] == expected["reload"] == {"result": "ok"}
            child_executables = [
                Path(shlex.split(command)[0]).name.lower()
                for command in actual["descendants"]
            ]
            assert not any(name.startswith("python") for name in child_executables)
    finally:
        origin.close()

    artifacts = [
        path
        for path in (tmp_path / "run-cache").rglob("__main__")
        if path.is_file() and os.access(path, os.X_OK)
    ]
    assert len(artifacts) == 1, artifacts
    _assert_no_libpython(artifacts[0])
