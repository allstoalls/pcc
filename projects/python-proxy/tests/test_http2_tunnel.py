"""TLS/HTTP2 tests use Node's independent client/server, not pproxy's H2 codec."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from test_proxy_stability import network, run, settled
from pproxy import server


async def exercise(net, tls_fixture, mode, case, duration_ms=200):
    node, cert, key = tls_fixture
    proxy = await net.proxy()
    process = await asyncio.create_subprocess_exec(
        node, str(Path(__file__).with_name("h2_tunnel_fixture.mjs")),
        str(proxy), mode, case, str(cert), str(key), str(duration_ms),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), duration_ms / 1000 + 11)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    result = json.loads(stdout)
    assert result["alpn"] == "h2" and result["authorized"]
    assert not result["sessionErrors"]
    assert len(result["results"]) == 8
    for item in result["results"]:
        if case == "reset" and item["index"] == 0:
            assert item["rstCode"] == 2 and item["error"], result
        else:
            assert item["status"] == 200 and item["correctBody"] and not item["error"], item
    if case == "goaway":
        # RFC 9113 section 6.8 allows additional GOAWAY during shutdown.
        assert result["goaway"]
        assert all(event == {"code": 0, "lastStreamID": 15} for event in result["goaway"])
    else:
        assert not result["goaway"]
    return result


@pytest.fixture(scope="module")
def tls_fixture(tmp_path_factory):
    node, openssl = shutil.which("node"), shutil.which("openssl")
    if not node or not openssl:
        pytest.skip("TLS/HTTP2 integration requires Node and openssl")
    directory = tmp_path_factory.mktemp("pproxy-local-tls")
    cert, key = directory / "cert.pem", directory / "key.pem"
    result = subprocess.run([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    key.chmod(0o600)
    return node, cert, key


@pytest.mark.parametrize("mode", ["direct", "socks5", "http"])
@pytest.mark.parametrize("case", ["multiplex", "reset", "goaway", "stream"])
def test_verified_tls_http2_tunnel(tls_fixture, mode, case):
    async def scenario():
        async with network() as net:
            await exercise(net, tls_fixture, mode, case)
            await settled()
            assert not server.ACTIVE_CHANNEL_WRITERS
    run(scenario())


@pytest.mark.soak
def test_long_lived_tls_http2_idle_and_heartbeat(tls_fixture, record_property):
    seconds = float(os.environ.get("PPROXY_SOAK_SECONDS", "245"))
    assert 65 <= seconds <= 3600, "Soak must exceed the 60-second handshake deadline"
    async def scenario():
        async with network() as net:
            jobs = [asyncio.create_task(exercise(net, tls_fixture, mode, case, int(seconds * 1000)))
                    for mode in ("direct", "socks5", "http") for case in ("long_idle", "long_stream")]
            start = asyncio.get_running_loop().time()
            try:
                while not all(job.done() for job in jobs):
                    done, _ = await asyncio.wait(jobs, timeout=30, return_when=asyncio.FIRST_EXCEPTION)
                    for job in done:
                        if job.exception():
                            raise job.exception()
                    print(json.dumps({"elapsed_seconds":round(asyncio.get_running_loop().time()-start),
                                      "finished_sessions":sum(job.done() for job in jobs),"total_sessions":6}), flush=True)
                results = [job.result() for job in jobs]
                for result in results:
                    for stream in result["results"]:
                        assert stream["durationMs"] >= seconds * 1000 - 100
                        if result["scenario"] == "long_idle":
                            assert stream["maxDataGapMs"] >= seconds * 1000 - 1000
                        else:
                            assert stream["maxDataGapMs"] < seconds * 1000 / 8 + 2000
                record_property("duration_seconds", seconds)
                record_property("http2_sessions", 6)
                record_property("verified_streams", 48)
                record_property("results", json.dumps(results))
                await settled()
                assert not server.ACTIVE_CHANNEL_WRITERS
            finally:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), seconds + 20))
