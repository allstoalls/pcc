"""Sequential channel canary for the pcc-owned virtual-thread runtime."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc1_gate import find_current_pcc1


REPO = Path(__file__).resolve().parents[2]


CHANNEL_SOURCE = textwrap.dedent(
    '''
    import pcc.virtual_thread as vt

    def bounded_sender(tx) -> None:
        first = vt.send(tx, 10)
        second = vt.send(tx, 11)
        closed = vt.close_sender(tx)
        print("bounded-send", first, second, closed)

    def bounded_receiver(rx) -> None:
        s0, v0 = vt.recv(rx)
        print("bounded-recv", s0, v0)
        s1, v1 = vt.recv(rx)
        print("bounded-recv", s1, v1)
        s2, v2 = vt.recv(rx)
        print("bounded-eof", s2, v2)

    def closing_sender(tx) -> None:
        first = vt.send(tx, 20)
        second = vt.send(tx, 21)
        print("receiver-close-send", first, second)

    def close_rx(rx) -> None:
        print("receiver-close", vt.close_receiver(rx))

    def send_none(tx) -> None:
        print("oneshot-send", vt.send(tx, None), vt.close_sender(tx))

    def recv_none(rx) -> None:
        status, value = vt.recv(rx)
        print("oneshot-recv", status, value is None)

    def close_tx(tx) -> None:
        print("sender-close", vt.close_sender(tx))

    def terminal_recv(label, rx) -> None:
        status, value = vt.recv(rx)
        print(label, status, value)

    def send_value(tx, value: int) -> None:
        print("send-value", value, vt.send(tx, value))
        vt.close_sender(tx)

    def select_value(label, left, right) -> None:
        winner, status, value = vt.select2(left, right)
        print(label, winner, status, value)

    def main() -> None:
        tx, rx = vt.mpsc(1)
        sender = vt.spawn(bounded_sender, tx)
        receiver = vt.spawn(bounded_receiver, rx)
        vt.run(1, 64)
        print("bounded-outcomes", vt.outcome(sender), vt.outcome(receiver))

        close_tx0, close_rx0 = vt.mpsc(1)
        blocked = vt.spawn(closing_sender, close_tx0)
        closer = vt.spawn(close_rx, close_rx0)
        vt.run(1, 64)
        print("receiver-close-outcomes", vt.outcome(blocked), vt.outcome(closer))

        one_tx, one_rx = vt.oneshot()
        one_sender = vt.spawn(send_none, one_tx)
        one_receiver = vt.spawn(recv_none, one_rx)
        vt.run(1, 64)
        print("oneshot-outcomes", vt.outcome(one_sender), vt.outcome(one_receiver))

        empty_tx, empty_rx = vt.oneshot()
        empty_closer = vt.spawn(close_tx, empty_tx)
        empty_receiver = vt.spawn(terminal_recv, "oneshot-eof", empty_rx)
        vt.run(1, 64)
        print("oneshot-eof-outcomes", vt.outcome(empty_closer), vt.outcome(empty_receiver))

        left_tx, left_rx = vt.mpsc(1)
        right_tx, right_rx = vt.mpsc(1)
        right_select = vt.spawn(select_value, "select-right", left_rx, right_rx)
        right_sender = vt.spawn(send_value, right_tx, 42)
        vt.run(1, 64)
        left_sender = vt.spawn(send_value, left_tx, 41)
        left_receiver = vt.spawn(terminal_recv, "select-loser", left_rx)
        vt.run(1, 64)
        print("select-right-outcomes", vt.outcome(right_select), vt.outcome(right_sender), vt.outcome(left_sender), vt.outcome(left_receiver))

        ready_left_tx, ready_left_rx = vt.mpsc(1)
        ready_right_tx, ready_right_rx = vt.mpsc(1)
        ready_left_sender = vt.spawn(send_value, ready_left_tx, 50)
        ready_right_sender = vt.spawn(send_value, ready_right_tx, 51)
        vt.run(1, 64)
        left_select = vt.spawn(select_value, "select-left", ready_left_rx, ready_right_rx)
        right_receiver = vt.spawn(terminal_recv, "select-left-loser", ready_right_rx)
        vt.run(1, 64)
        print("select-left-outcomes", vt.outcome(left_select), vt.outcome(right_receiver))

        cancel_left_tx, cancel_left_rx = vt.mpsc(1)
        cancel_right_tx, cancel_right_rx = vt.mpsc(1)
        cancelled = vt.spawn(select_value, "select-cancelled-unreachable", cancel_left_rx, cancel_right_rx)
        vt.run(1, 1)
        print("select-cancel", vt.cancel(cancelled))
        vt.run(1, 64)
        cancel_left_sender = vt.spawn(send_value, cancel_left_tx, 60)
        cancel_right_sender = vt.spawn(send_value, cancel_right_tx, 61)
        cancel_left_receiver = vt.spawn(terminal_recv, "select-cancel-left", cancel_left_rx)
        cancel_right_receiver = vt.spawn(terminal_recv, "select-cancel-right", cancel_right_rx)
        vt.run(1, 128)
        print("select-cancel-outcomes", vt.outcome(cancelled), vt.outcome(cancel_left_receiver), vt.outcome(cancel_right_receiver))

    if __name__ == "__main__":
        main()
    '''
).lstrip()


def _assert_channel_output(stdout: str) -> None:
    lines = stdout.strip().splitlines()
    assert "bounded-recv 1 10" in lines
    assert "bounded-recv 1 11" in lines
    assert "bounded-eof 2 None" in lines
    assert "bounded-send True True True" in lines
    assert "receiver-close-send True False" in lines
    assert "oneshot-recv 1 True" in lines
    assert "oneshot-eof 2 None" in lines
    assert "select-right 1 1 42" in lines
    assert "select-loser 1 41" in lines
    assert "select-left 0 1 50" in lines
    assert "select-left-loser 1 51" in lines
    assert "select-cancel True" in lines
    assert "select-cancel-left 1 60" in lines
    assert "select-cancel-right 1 61" in lines
    assert "select-cancelled-unreachable" not in stdout
    assert lines[-1] == "select-cancel-outcomes 3 1 1"


def test_bounded_mpsc_oneshot_select2_sequential_contract(
    tmp_path: Path,
    monkeypatch,
    pcc_runtime_archive: Path,
) -> None:
    source = tmp_path / "virtual_thread_channels.py"
    executable = tmp_path / "virtual_thread_channels"
    source.write_text(CHANNEL_SOURCE, encoding="utf-8")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    compile_python(
        str(source),
        str(executable),
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ran = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    _assert_channel_output(ran.stdout)


@pytest.mark.integration
@pytest.mark.pcc_gate(probe="pcc1")
@pytest.mark.xdist_group(name="pcc1_vthread_channels")
def test_current_pcc1_self_no_libpython_channels(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> None:
    """Compile once with current pcc1/self, then exercise channels on GC0..4."""
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail("a source-current pcc1 is required for the channel gate")

    source = tmp_path / "current_pcc1_virtual_thread_channels.py"
    executable = tmp_path / "current_pcc1_virtual_thread_channels"
    source.write_text(CHANNEL_SOURCE, encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)
    command = [
        str(pcc1),
        "--backend",
        "self",
        "--python-libpython=off",
        "--ir-scaffold=on",
        str(source),
        "-o",
        str(executable),
    ]
    built = subprocess.run(
        command,
        cwd=REPO,
        env=environment,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    assert executable.is_file()

    for gc_backend in range(5):
        run_environment = dict(environment)
        run_environment["PCC_GC_BACKEND"] = str(gc_backend)
        ran = subprocess.run(
            [str(executable)],
            cwd=REPO,
            env=run_environment,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert ran.returncode == 0, (
            "GC" + str(gc_backend) + ": " + ran.stdout + ran.stderr
        )
        _assert_channel_output(ran.stdout)
