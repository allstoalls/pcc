"""Sequential TCP contract for the pcc-owned virtual-thread reactor."""

from __future__ import annotations

from tests.owned_ir_validation import verify_ir_text

import os
from pathlib import Path
import socket
import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc1_gate import find_current_pcc1


REPO = Path(__file__).resolve().parents[2]


def _tcp_source(port: int) -> str:
    return textwrap.dedent(
        f'''
        import pcc.virtual_thread as vt
        from pcc.extern import c_int64, extern

        PORT = {port}
        MESSAGE = b"pcc-sequential-net"
        CLOSE_EVENTS = []
        scheduler_root_count = extern("pcc_gc_scheduler_root_count", (), c_int64)
        timer_count = extern("py_virtual_thread_timer_count", (), c_int64)
        io_wait_count = extern("py_virtual_thread_io_wait_count", (), c_int64)

        def recv_exact(fd: int, size: int) -> bytes:
            result = b""
            while len(result) < size:
                chunk = vt.tcp_recv(fd, size - len(result), 2000)
                if len(chunk) == 0:
                    return result
                result = result + chunk
            return result

        def server(listener: int) -> int:
            peer = vt.tcp_accept(listener, 2000)
            payload = recv_exact(peer, len(MESSAGE))
            vt.tcp_send_all(peer, payload, 2000)
            vt.tcp_close(peer)
            return len(payload)

        def client() -> int:
            fd = vt.tcp_connect("127.0.0.1", PORT, 2000)
            vt.tcp_send_all(fd, MESSAGE, 2000)
            echoed = recv_exact(fd, len(MESSAGE))
            vt.tcp_close(fd)
            return len(echoed)

        def close_waiter(listener: int) -> int:
            try:
                peer = vt.tcp_accept(listener, 2000)
                vt.tcp_close(peer)
                return 20
            except OSError:
                CLOSE_EVENTS.append(1)
                return len(CLOSE_EVENTS)

        def close_owner(listener: int) -> int:
            vt.tcp_close(listener)
            replacement = vt.tcp_listen("127.0.0.1", 0, 4)
            reused = replacement == listener
            vt.tcp_close(replacement)
            if reused:
                return 1
            return 0

        def timeout_waiter(listener: int) -> int:
            try:
                peer = vt.tcp_accept(listener, 0)
                vt.tcp_close(peer)
                return 20
            except OSError:
                return 1

        def cancel_waiter(listener: int) -> int:
            peer = vt.tcp_accept(listener, 60000)
            vt.tcp_close(peer)
            return 20

        def main() -> int:
            root_baseline = scheduler_root_count()
            timer_baseline = timer_count()
            io_baseline = io_wait_count()
            listener = vt.tcp_listen("127.0.0.1", PORT, 16)
            server_task = vt.spawn(server, listener)
            client_task = vt.spawn(client)
            vt.run(1, 4096)
            vt.tcp_close(listener)
            if vt.outcome(server_task) != vt.OUTCOME_RETURNED:
                return 10
            if vt.outcome(client_task) != vt.OUTCOME_RETURNED:
                return 11
            if vt.result(server_task) != len(MESSAGE):
                return 12
            if vt.result(client_task) != len(MESSAGE):
                return 13
            race_listener = vt.tcp_listen("127.0.0.1", 0, 4)
            close_waiter_task = vt.spawn(close_waiter, race_listener)
            vt.run(1, 1)
            if io_wait_count() != io_baseline + 1:
                return 25
            close_owner_task = vt.spawn(close_owner, race_listener)
            vt.run(1, 4096)
            if vt.outcome(close_waiter_task) != vt.OUTCOME_RETURNED:
                return 14
            if vt.outcome(close_owner_task) != vt.OUTCOME_RETURNED:
                return 15
            if vt.result(close_waiter_task) != 1:
                return 16
            if vt.result(close_owner_task) != 1:
                return 17
            timeout_listener = vt.tcp_listen("127.0.0.1", 0, 4)
            timeout_task = vt.spawn(timeout_waiter, timeout_listener)
            vt.run(1, 64)
            vt.tcp_close(timeout_listener)
            if vt.outcome(timeout_task) != vt.OUTCOME_RETURNED:
                return 18
            if vt.result(timeout_task) != 1:
                return 19
            cancel_listener = vt.tcp_listen("127.0.0.1", 0, 4)
            cancel_task = vt.spawn(cancel_waiter, cancel_listener)
            vt.run(1, 1)
            if io_wait_count() != io_baseline + 1:
                return 26
            if not vt.cancel(cancel_task):
                return 20
            vt.run(1, 64)
            vt.tcp_close(cancel_listener)
            if vt.outcome(cancel_task) != vt.OUTCOME_CANCELLED:
                return 21
            if scheduler_root_count() != root_baseline:
                return 22
            if timer_count() != timer_baseline:
                return 23
            if io_wait_count() != io_baseline:
                return 24
            print("SEQUENTIAL_TCP_OK", vt.io_backend())
            return 0

        main()
        '''
    ).lstrip()


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


def _assert_echo(stdout: str) -> None:
    expected_backend = 1 if os.uname().sysname == "Darwin" else 2
    assert stdout.strip() == f"SEQUENTIAL_TCP_OK {expected_backend}"


def test_darwin_sequential_tcp_contract(tmp_path: Path, monkeypatch) -> None:
    """Lower the flat API to observe/park/retry with managed frame state."""
    source = tmp_path / "sequential_tcp_contract.py"
    llvm_ir = tmp_path / "sequential_tcp_contract.ll"
    source.write_text(_tcp_source(43123), encoding="utf-8")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    text = llvm_ir.read_text(encoding="utf-8")
    # Frame-root addresses used by sibling retry/cleanup blocks must be
    # defined in a block that dominates every use.
    verify_ir_text(text)
    for symbol in (
        "py_virtual_thread_tcp_listen",
        "py_virtual_thread_tcp_accept_observe",
        "py_virtual_thread_tcp_connect_start",
        "py_virtual_thread_tcp_connect_observe",
        "py_virtual_thread_tcp_recv_observe",
        "py_virtual_thread_tcp_send_observe",
        "py_virtual_thread_tcp_close",
        "py_virtual_thread_io_resource_generation",
        "py_virtual_thread_block_on_fd_generation",
        "py_virtual_thread_tcp_register_accepted",
        "py_virtual_thread_tcp_deadline",
        "py_virtual_thread_tcp_remaining",
        "pcc_gc_store_root",
    ):
        assert "@" + symbol in text
    assert "usleep" not in text
    assert "nanosleep" not in text

    runtime_source = (
        REPO / "pcc" / "runtime" / "py" / "py_asyncio_io_runtime.py"
    ).read_text(encoding="utf-8")
    for function_name, syscall_name in (
        ("py_virtual_thread_tcp_accept_observe", "pcc_platform_tcp_accept_observe"),
        ("py_virtual_thread_tcp_connect_observe", "pcc_platform_socket_connect_observe"),
        ("py_virtual_thread_tcp_recv_observe", "pcc_platform_socket_read_observe"),
        ("py_virtual_thread_tcp_send_observe", "pcc_platform_socket_write_observe"),
    ):
        start = runtime_source.index("def " + function_name + "(")
        end = runtime_source.find("\n@c_abi_export", start + 1)
        section = runtime_source[start:end]
        begin_pos = section.index("py_virtual_thread_io_resource_operation_begin(")
        syscall_pos = section.index(syscall_name + "(")
        end_pos = section.index(
            "py_virtual_thread_io_resource_operation_end()", syscall_pos
        )
        assert begin_pos < syscall_pos < end_pos
    accept_start = runtime_source.index(
        "def py_virtual_thread_tcp_accept_observe("
    )
    accept_end = runtime_source.find("\n@c_abi_export", accept_start + 1)
    accept_section = runtime_source[accept_start:accept_end]
    assert accept_section.index("ptr_is_null(output_fd)") < accept_section.index(
        "py_virtual_thread_io_resource_operation_begin("
    )
    connect_start = runtime_source.index(
        "def py_virtual_thread_tcp_connect_start("
    )
    connect_end = runtime_source.find("\n@c_abi_export", connect_start + 1)
    connect_section = runtime_source[connect_start:connect_end]
    assert "ptr_is_null(output_fd)" in connect_section
    close_start = runtime_source.index("def py_virtual_thread_tcp_close(")
    close_end = runtime_source.find("\n@c_abi_export", close_start + 1)
    close_section = runtime_source[close_start:close_end]
    assert close_section.index("py_virtual_thread_io_resource_close_begin(") < close_section.index("pcc_platform_close(")
    assert close_section.index("pcc_platform_close(") < close_section.index("py_virtual_thread_io_resource_operation_end()")


def test_sequential_tcp_gc_matrix(
    tmp_path: Path,
    monkeypatch,
    pcc_runtime_archive: Path,
) -> None:
    """Compile once from current source and execute the flat API on GC0..4."""
    source = tmp_path / "sequential_tcp_gc.py"
    executable = tmp_path / "sequential_tcp_gc"
    source.write_text(_tcp_source(_reserve_port()), encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    compile_python(
        str(source),
        str(executable),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        environment = dict(os.environ)
        environment.pop("LC_ALL", None)
        environment["PCC_GC_BACKEND"] = str(backend)
        ran = subprocess.run(
            [str(executable)],
            cwd=REPO,
            env=environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert ran.returncode == 0, (
            "GC" + str(backend) + ": " + ran.stdout + ran.stderr
        )
        _assert_echo(ran.stdout)


@pytest.mark.integration
@pytest.mark.pcc_gate(probe="pcc1")
@pytest.mark.xdist_group(name="pcc1_sequential_tcp")
def test_current_pcc1_self_no_libpython_sequential_tcp_echo(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    """Compile the same finite API with source-current pcc1/self."""
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail("a source-current pcc1 is required for sequential TCP")
    source = tmp_path / "pcc1_sequential_tcp.py"
    executable = tmp_path / "pcc1_sequential_tcp"
    source.write_text(_tcp_source(_reserve_port()), encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    built = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    for backend in range(5):
        run_environment = dict(environment)
        run_environment["PCC_GC_BACKEND"] = str(backend)
        ran = subprocess.run(
            [str(executable)],
            cwd=REPO,
            env=run_environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert ran.returncode == 0, (
            "GC" + str(backend) + ": " + ran.stdout + ran.stderr
        )
        _assert_echo(ran.stdout)
