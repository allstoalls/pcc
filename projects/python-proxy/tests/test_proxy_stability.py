"""Offline integration tests: real TCP sockets, ephemeral loopback ports only."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import socket
import errno

import pytest

from pproxy import proto, server


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=12))


class Network:
    def __init__(self):
        self.listeners = []
        self.tasks = set()
        self.writers = []
        self.errors = []

    async def listen(self, handler, host="127.0.0.1"):
        async def accepted(reader, writer):
            task = asyncio.current_task()
            self.tasks.add(task)
            self.writers.append(writer)
            try:
                await handler(reader, writer)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.tasks.discard(task)

        listener = await asyncio.start_server(accepted, host, 0)
        self.listeners.append(listener)
        return listener.sockets[0].getsockname()[1]

    async def proxy(self, remotes=(), users=()):
        async def handler(reader, writer):
            await server.stream_handler(
                reader, writer, unix=False, lbind=None,
                protos=[proto.HTTP(None), proto.Socks5(None)],
                rserver=list(remotes), cipher=None, sslserver=None,
                users=list(users), verbose=lambda message: None,
            )

        return await self.listen(handler)

    async def tcp(self, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        return reader, writer

    async def close(self):
        for listener in self.listeners:
            listener.close()
        for writer in self.writers:
            writer.close()
        server.close_active_channel_writers()
        pending = set(self.tasks) | set(server.ACTIVE_CHANNEL_TASKS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(w.wait_closed() for w in self.writers), return_exceptions=True)
        await asyncio.gather(*(s.wait_closed() for s in self.listeners))
        await asyncio.sleep(0)
        assert not self.errors, repr(self.errors)


@asynccontextmanager
async def network():
    assert not server.ACTIVE_CHANNEL_TASKS
    assert not server.ACTIVE_CHANNEL_WRITERS
    server.AuthTable._auth.clear()
    server.AuthTable._user.clear()
    net = Network()
    try:
        yield net
    finally:
        await net.close()
        server.AuthTable._auth.clear()
        server.AuthTable._user.clear()


async def echo(reader, writer):
    try:
        while data := await reader.read(16384):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


def address(host, port, kind="domain"):
    if kind == "ipv4":
        encoded = b"\x01" + socket.inet_aton(host)
    elif kind == "ipv6":
        encoded = b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    else:
        raw = host.encode("ascii")
        encoded = b"\x03" + bytes([len(raw)]) + raw
    return encoded + port.to_bytes(2, "big")


async def socks_reply(reader):
    header = await reader.readexactly(4)
    assert header[0] == 5 and header[2] == 0
    if header[3] == 1:
        size = 4
    elif header[3] == 4:
        size = 16
    else:
        assert header[3] == 3
        size = (await reader.readexactly(1))[0]
    await reader.readexactly(size + 2)
    return header[1]


async def tunnel(net, proxy_port, target_port, mode, payload=b"", fragmented=False, host="127.0.0.1"):
    reader, writer = await net.tcp(proxy_port)
    if mode == "http":
        request = (f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
                   f"Host: 127.0.0.1:{target_port}\r\n\r\n").encode()
    else:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        request = b"\x05\x01\x00" + address(host, target_port, mode)
    if fragmented:
        for byte in request[:-1]:
            writer.write(bytes([byte]))
            await writer.drain()
            await asyncio.sleep(0)
        writer.write(request[-1:] + payload)
    else:
        writer.write(request + payload)
    await writer.drain()
    if mode == "http":
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.split(b" ", 2)[1] == b"200", response
    else:
        assert await socks_reply(reader) == 0
    return reader, writer


async def settled():
    for _ in range(100):
        if not server.ACTIVE_CHANNEL_TASKS:
            return
        await asyncio.sleep(0.005)
    assert not server.ACTIVE_CHANNEL_TASKS, "Tunnel tasks retained after close"


@pytest.mark.parametrize("mode", ["domain", "ipv4", "http"])
@pytest.mark.parametrize("fragmented", [False, True])
def test_fragmented_handshake_preserves_pipelined_binary_payload(mode, fragmented):
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            proxy = await net.proxy()
            payload = bytes(range(256)) * 8
            reader, writer = await tunnel(net, proxy, target, mode, payload, fragmented)
            assert await reader.readexactly(len(payload)) == payload
            writer.close()
            await settled()
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
def test_concurrent_large_transfers_are_not_truncated_or_mixed(mode):
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            proxy = await net.proxy()
            async def transfer(index):
                reader, writer = await tunnel(net, proxy, target, mode)
                payload = hashlib.sha256(str(index).encode()).digest() * 32768
                async def send():
                    for offset in range(0, len(payload), 8192):
                        writer.write(payload[offset:offset + 8192])
                        await writer.drain()
                sending = asyncio.create_task(send())
                try:
                    received = await reader.readexactly(len(payload))
                    assert hashlib.sha256(received).digest() == hashlib.sha256(payload).digest()
                    await sending
                finally:
                    sending.cancel()
                    await asyncio.gather(sending, return_exceptions=True)
                    writer.close()
            await asyncio.gather(*(transfer(index) for index in range(8)))
            await settled()
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
@pytest.mark.parametrize("close_side", ["client", "upstream"])
def test_peer_close_releases_both_forwarding_tasks(mode, close_side):
    async def scenario():
        async with network() as net:
            peer = asyncio.Future()
            async def target_handler(reader, writer):
                peer.set_result((reader, writer))
            target = await net.listen(target_handler)
            proxy = await net.proxy()
            reader, writer = await tunnel(net, proxy, target, mode)
            remote_reader, remote_writer = await peer
            if close_side == "client":
                writer.close()
                assert await remote_reader.read(1) == b""
            else:
                remote_writer.close()
                assert await reader.read(1) == b""
            await settled()
    run(scenario())


def test_completed_tunnels_release_writer_registry():
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            proxy = await net.proxy()
            for _ in range(5):
                reader, writer = await tunnel(net, proxy, target, "domain", b"ok")
                assert await reader.readexactly(2) == b"ok"
                writer.close()
                await settled()
            assert server.ACTIVE_CHANNEL_WRITERS == [], "Closed writers remain globally rooted"
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
def test_success_reply_waits_for_actual_upstream_connection(mode):
    async def scenario():
        async with network() as net:
            gate, entered = asyncio.Event(), asyncio.Event()
            class DelayedRoute(server.ProxyDirect):
                async def open_connection(self, *args, **kwargs):
                    entered.set()
                    await gate.wait()
                    return await super().open_connection(*args, **kwargs)
            target = await net.listen(echo)
            proxy = await net.proxy([DelayedRoute()])
            pending = asyncio.create_task(tunnel(net, proxy, target, mode))
            try:
                await entered.wait()
                done, _ = await asyncio.wait([pending], timeout=0.05)
                assert not done, "Proxy reported success before connecting to destination"
                gate.set()
                reader, writer = await pending
                writer.write(b"ready")
                await writer.drain()
                assert await reader.readexactly(5) == b"ready"
            finally:
                gate.set()
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    run(scenario())


@pytest.mark.parametrize("error,expected", [
    (socket.gaierror(socket.EAI_NONAME, "controlled DNS failure"), 4),
    (ConnectionRefusedError(61, "controlled refusal"), 5),
    (TimeoutError("controlled timeout"), 4),
    (OSError(errno.ENETUNREACH, "controlled network unreachable"), 3),
    (PermissionError(errno.EACCES, "controlled denial"), 2),
])
def test_socks_destination_failure_is_not_reported_as_success(error, expected):
    async def scenario():
        async with network() as net:
            class FailedRoute(server.ProxyDirect):
                async def open_connection(self, *args, **kwargs):
                    raise error
            proxy = await net.proxy([FailedRoute()])
            reader, writer = await net.tcp(proxy)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(b"\x05\x01\x00" + address("unused.test", 443))
            await writer.drain()
            assert await socks_reply(reader) == expected
            assert await reader.read() == b""
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
@pytest.mark.parametrize("upstream_protocol", ["socks5", "http"])
def test_two_proxy_chain_preserves_payload(mode, upstream_protocol):
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            exit_port = await net.proxy()
            remote = server.proxies_by_uri(f"{upstream_protocol}://127.0.0.1:{exit_port}")
            entry = await net.proxy([remote])
            data = bytes(range(256)) * 2048
            reader, writer = await tunnel(net, entry, target, mode, data)
            assert await reader.readexactly(len(data)) == data
            writer.close()
            await settled()
            assert not server.ACTIVE_CHANNEL_WRITERS
    run(scenario())


def test_socks_chain_preserves_destination_dns_failure():
    async def scenario():
        async with network() as net:
            class FailedRoute(server.ProxyDirect):
                async def open_connection(self, *args, **kwargs):
                    raise socket.gaierror(socket.EAI_NONAME, "controlled DNS failure")
            exit_port = await net.proxy([FailedRoute()])
            entry = await net.proxy([server.proxies_by_uri(f"socks5://127.0.0.1:{exit_port}")])
            reader, writer = await net.tcp(entry)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(b"\x05\x01\x00" + address("unused.test", 443))
            await writer.drain()
            assert await socks_reply(reader) == 4
            assert await reader.read() == b""
            await settled()
    run(scenario())


@pytest.mark.parametrize("status", [407, 403, 502])
def test_http_upstream_rejection_never_becomes_socks_success(status):
    async def scenario():
        async with network() as net:
            async def reject(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(f"HTTP/1.1 {status} Rejected\r\nContent-Length: 0\r\n\r\n".encode())
                await writer.drain()
                writer.close()
            exit_port = await net.listen(reject)
            entry = await net.proxy([server.proxies_by_uri(f"http://127.0.0.1:{exit_port}")])
            reader, writer = await net.tcp(entry)
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(b"\x05\x01\x00" + address("unused.test", 443))
            await writer.drain()
            assert await socks_reply(reader) != 0
            assert await reader.read() == b""
    run(scenario())


@pytest.mark.parametrize("partial", [b"", b"C", b"CONNE", b"\x05", b"\x05\x01", b"\x05\x01\x00\x05\x01\x00\x03\x05ab"])
def test_abandoned_handshake_does_not_break_next_client(partial):
    async def scenario():
        async with network() as net:
            proxy = await net.proxy()
            target = await net.listen(echo)
            _, abandoned = await net.tcp(proxy)
            abandoned.write(partial)
            await abandoned.drain()
            abandoned.close()
            await abandoned.wait_closed()
            reader, writer = await tunnel(net, proxy, target, "domain", b"healthy")
            assert await reader.readexactly(7) == b"healthy"
            writer.close()
            await settled()
            assert not server.ACTIVE_CHANNEL_WRITERS
    run(scenario())


@pytest.mark.parametrize("mode", ["domain", "http"])
def test_established_tunnel_survives_handshake_timeout(monkeypatch, mode):
    async def scenario():
        async with network() as net:
            target = await net.listen(echo)
            proxy = await net.proxy()
            reader, writer = await tunnel(net, proxy, target, mode)
            monkeypatch.setattr(server, "SOCKET_TIMEOUT", 0.05)
            await asyncio.sleep(0.2)
            writer.write(b"still-alive")
            await writer.drain()
            assert await reader.readexactly(11) == b"still-alive"
    run(scenario())


def test_socks_ipv6_destination():
    async def scenario():
        async with network() as net:
            try:
                target = await net.listen(echo, host="::1")
            except OSError as error:
                if error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    pytest.skip("IPv6 loopback unavailable")
                raise
            proxy = await net.proxy()
            reader, writer = await tunnel(net, proxy, target, "ipv6", b"ipv6", host="::1")
            assert await reader.readexactly(4) == b"ipv6"
    run(scenario())


def test_builtin_echo_sends_socks_reply_before_payload():
    async def scenario():
        async with network() as net:
            proxy = await net.proxy()
            reader, writer = await tunnel(net, proxy, 1, "domain", b"echo", host="echo")
            assert await reader.readexactly(4) == b"echo"
            writer.close()
            await settled()
            assert not server.ACTIVE_CHANNEL_WRITERS
    run(scenario())
