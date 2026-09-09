import asyncio
import base64
import hashlib
import re
import signal
import sys
from pathlib import Path

import pytest

from pproxy import server
from test_proxy_stability import network, run, echo, tunnel, address, socks_reply, settled


@pytest.mark.parametrize("mode", ["socks5", "http"])
@pytest.mark.parametrize("valid", [True, False])
def test_authentication_accepts_valid_and_rejects_invalid_credentials(mode, valid):
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            proxy = await net.proxy(users=[b"probe:correct"])
            reader, writer = await net.tcp(proxy)
            password = b"correct" if valid else b"wrong"
            if mode == "socks5":
                writer.write(b"\x05\x01\x02")
                await writer.drain()
                assert await reader.readexactly(2) == b"\x05\x02"
                writer.write(b"\x01\x05probe" + bytes([len(password)]) + password)
                await writer.drain()
                result = await reader.read(2)
                if not valid:
                    assert result != b"\x01\x00"
                    assert await reader.read() == b""
                    return
                assert result == b"\x01\x00"
                writer.write(b"\x05\x01\x00" + address("127.0.0.1", target))
                await writer.drain()
                assert await socks_reply(reader) == 0
            else:
                token = base64.b64encode(b"probe:" + password).decode()
                writer.write((f"CONNECT 127.0.0.1:{target} HTTP/1.1\r\n"
                              f"Host: 127.0.0.1:{target}\r\nProxy-Authorization: Basic {token}\r\n\r\n").encode())
                await writer.drain()
                result = await reader.readuntil(b"\r\n\r\n")
                assert result.split(b" ")[1] == (b"200" if valid else b"407")
                if not valid:
                    assert await reader.read() == b""
                    return
            writer.write(b"authenticated")
            await writer.drain()
            assert await reader.readexactly(13) == b"authenticated"
    run(scenario())


@pytest.mark.parametrize("methods,users", [(b"", []), (b"\x01", []), (b"\x02", []), (b"\x00", [b"probe:correct"])])
def test_socks_rejects_when_no_auth_method_is_shared(methods, users):
    async def scenario():
        async with network() as net:
            proxy = await net.proxy(users=users)
            reader, writer = await net.tcp(proxy)
            writer.write(b"\x05" + bytes([len(methods)]) + methods)
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\xff"
            assert await reader.read() == b""
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
@pytest.mark.parametrize("side", ["client", "upstream"])
def test_abrupt_disconnect_does_not_break_other_tunnels(mode, side):
    async def scenario():
        async with network() as net:
            peer = asyncio.Future()
            async def interrupted(reader, writer):
                peer.set_result(writer)
                await reader.read()
            bad_target = await net.listen(interrupted)
            healthy_target = await net.listen(echo)
            proxy = await net.proxy()
            healthy_reader, healthy_writer = await tunnel(net, proxy, healthy_target, mode)
            bad_reader, bad_writer = await tunnel(net, proxy, bad_target, mode)
            remote_writer = await peer
            (bad_writer if side == "client" else remote_writer).transport.abort()
            try:
                assert await bad_reader.read() == b""
            except ConnectionResetError:
                pass
            healthy_writer.write(b"unaffected")
            await healthy_writer.drain()
            assert await healthy_reader.readexactly(10) == b"unaffected"
            healthy_writer.close()
            await settled()
            assert not server.ACTIVE_CHANNEL_WRITERS
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
def test_slow_receiver_backpressure_preserves_all_bytes(mode):
    async def scenario():
        async with network() as net:
            payload = bytes(range(256)) * 8192
            digest = asyncio.Future()
            async def slow(reader, writer):
                received = hashlib.sha256()
                for _ in range(len(payload) // 8192):
                    received.update(await reader.readexactly(8192))
                    await asyncio.sleep(0.001)
                digest.set_result(received.digest())
                writer.write(b"complete")
                await writer.drain()
                writer.close()
            target = await net.listen(slow)
            proxy = await net.proxy()
            reader, writer = await tunnel(net, proxy, target, mode)
            writer.transport.set_write_buffer_limits(high=16384, low=8192)
            for offset in range(0, len(payload), 16384):
                writer.write(payload[offset:offset + 16384])
                await writer.drain()
            assert await reader.readexactly(8) == b"complete"
            assert await digest == hashlib.sha256(payload).digest()
            assert await reader.read() == b""
            await settled()
    run(scenario())


@pytest.mark.parametrize("connection_state", ["idle", "handshake", "active"])
def test_cli_sigint_exits_with_open_connections(connection_state):
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "pproxy", "-l", "http+socks5://127.0.0.1:0",
            cwd=Path(__file__).resolve().parents[1],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        writer = None
        try:
            for _ in range(5):
                line = await asyncio.wait_for(process.stdout.readline(), 3)
                match = re.search(rb"127\.0\.0\.1:(\d+)", line)
                if match:
                    break
            assert match, "CLI did not announce a bound port"
            if connection_state != "idle":
                reader, writer = await asyncio.open_connection("127.0.0.1", int(match[1]))
                if connection_state == "handshake":
                    writer.write(b"\x05\x01")
                else:
                    writer.write(b"\x05\x01\x00")
                    await writer.drain()
                    assert await reader.readexactly(2) == b"\x05\x00"
                    writer.write(b"\x05\x01\x00" + address("echo", 1))
                    await writer.drain()
                    assert await socks_reply(reader) == 0
                    writer.write(b"active")
                    await writer.drain()
                    assert await reader.readexactly(6) == b"active"
            process.send_signal(signal.SIGINT)
            _, stderr = await asyncio.wait_for(process.communicate(), 2)
            assert process.returncode == 0, stderr.decode()
            assert b"Task was destroyed" not in stderr
            assert b"never awaited" not in stderr
            if writer:
                try:
                    assert await reader.read() == b""
                except ConnectionResetError:
                    # Closing a socket with an unfinished request can send RST.
                    assert connection_state == "handshake"
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except ConnectionResetError:
                    assert connection_state == "handshake"
            if process.returncode is None:
                process.kill()
                await process.wait()
    run(scenario())
