"""Phase D3 — async / await contract.

Locks the contract for ``async def`` coroutine functions, ``await``
expressions, ``async for`` / ``async with``, and the underlying
coroutine state machine per ``docs/issues/python-data-model-gaps.md``
Phase D3.

D3 builds on D2 (generator state machine) since coroutines reuse the
same frame-suspension mechanism.

Sub-protocols (rough implementation order):

1. ``async def`` defines a coroutine function (calling it returns a
   coroutine object, doesn't execute the body)
2. ``await`` suspends and resumes a coroutine
3. asyncio-style event loop minimal: ``run`` / ``sleep(0)`` round-trip
4. ``async for`` over async iterators
5. ``async with`` for async context managers
6. ``__await__`` protocol on user objects
"""
from __future__ import annotations

import subprocess
import textwrap


def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from tests.native_provisioning import native_test_runtime_options

    runtime_options = native_test_runtime_options()
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", **runtime_options)
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# async def returns coroutine
# ---------------------------------------------------------------------------


def test_async_def_returns_coroutine_object(tmp_path):
    """Calling an ``async def`` function does NOT execute the body —
    it returns a coroutine object."""
    result = _compile_and_run(tmp_path, """
        async def foo():
            print("body-ran")
            return 1

        def main() -> None:
            c = foo()                          # body must NOT run yet
            print(type(c).__name__)            # 'coroutine'
            c.close()                          # tidy up
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert "body-ran" not in out
    assert out[0] == "coroutine"
    assert out[-1] == "ok"


# ---------------------------------------------------------------------------
# await with minimal event loop
# ---------------------------------------------------------------------------


def test_await_round_trip(tmp_path):
    """A coroutine can ``await`` another coroutine and observe its
    return value."""
    result = _compile_and_run(tmp_path, """
        import asyncio

        async def inner():
            return 42

        async def outer():
            x = await inner()
            return x + 1

        def main() -> None:
            print(asyncio.run(outer()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "43"


def test_await_result_survives_returned_nested_async_function(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        async def outer():
            fut = asyncio.Future()
            fut.set_result(b"payload")
            value = await fut

            async def nested(wait=False):
                if wait:
                    await asyncio.sleep(0)
                return "nested"

            return value, nested

        def main() -> None:
            value, nested = asyncio.run(outer())
            print(len(value), value[:3])
            print(type(nested()).__name__)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["7 b'pay'", "coroutine"]


def test_nested_async_closure_formats_captured_strings(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio
        import re
        import urllib.parse

        class Writer:
            def __init__(self):
                self.payload = b""

            def write(self, data):
                self.payload = data


        class HTTP:
            async def http_accept(self, user, method, path, authority, ver, lines, host, pauth, reply, authtable, users, httpget=None, **kw):
                url = urllib.parse.urlparse(path)
                if method == "CONNECT":
                    return user, "example.com", 443, None
                host_name, port = "example.com", 80
                newpath = url._replace(netloc="", scheme="").geturl()

                async def connected(writer):
                    writer.write(f"{method} {newpath} {ver}\\r\\n{lines}\\r\\n\\r\\n".encode())
                    return True

                return user, host_name, port, connected

        async def outer():
            http_line = re.compile("([^ ]+) +(.+?) +(HTTP/[^ ]+)$")
            lines = b"GET http://example.com/ HTTP/1.1\\r\\nHost: example.com\\r\\nProxy-Connection: Keep-Alive\\r\\n\\r\\n"
            headers = lines[:-4].decode().split("\\r\\n")
            method, path, ver = http_line.match(headers.pop(0)).groups()
            lines = "\\r\\n".join(i for i in headers if not i.startswith("Proxy-"))
            headers = dict(i.split(": ", 1) for i in headers if ": " in i)
            proto = HTTP()
            user, host_name, port, connected = await proto.http_accept(
                True, method, path, None, ver, lines, headers.get("Host", ""),
                headers.get("Proxy-Authorization"), None, None, None,
            )
            writer = Writer()
            ok = await connected(writer)
            print(writer.payload)
            print(host_name, port)
            return ok

        def main() -> None:
            print(asyncio.run(outer()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "b'GET / HTTP/1.1\\r\\nHost: example.com\\r\\n\\r\\n'",
        "example.com 80",
        "True",
    ]


def test_nested_async_closure_forwarded_through_method_parameter_keeps_writer(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class Writer:
            def __init__(self):
                self.payload = b""

            def write(self, data):
                self.payload = data


        class HTTP:
            async def accept(self, writer):
                async def reply(code, message, body=None, wait=False):
                    print(type(writer).__name__)
                    writer.write(message)
                    return True

                return await self.http_accept(reply)

            async def http_accept(self, reply):
                return lambda writer: reply(200, b"HTTP/1.1 200 Connection established\\r\\n\\r\\n")


        async def outer():
            client = Writer()
            connected = await HTTP().accept(client)
            ok = await connected(None)
            print(client.payload)
            return ok

        def main() -> None:
            print(asyncio.run(outer()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "Writer",
        "b'HTTP/1.1 200 Connection established\\r\\n\\r\\n'",
        "True",
    ]


def test_async_param_captured_by_returned_closure_is_a_cell(tmp_path):
    """A parameter a returned closure captures is a list cell for the whole
    body; a generator's factory must store that cell in the frame, or the
    closure's ``writer[0]`` read subscripts the argument itself."""
    result = _compile_and_run(tmp_path, """
        import asyncio

        class Writer:
            pass

        async def accept(writer, other):
            def sync_reply():
                return type(writer).__name__ + " " + str(other)
            async def async_reply():
                return type(writer).__name__ + "!"
            return sync_reply, async_reply

        async def outer():
            sync_reply, async_reply = await accept(Writer(), 5)
            print(sync_reply())
            print(await async_reply())

        def main() -> None:
            asyncio.run(outer())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["Writer 5", "Writer!"]


def test_cross_module_async_function_attr_call_returns_coroutine(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    mod = tmp_path / "moda.py"
    mod.write_text(
        "async def accept():\n"
        "    return (1, 2, 3)\n",
        encoding="utf-8",
    )
    src = tmp_path / "prog.py"
    src.write_text(
        "import asyncio\n"
        "import moda\n"
        "async def main_async():\n"
        "    x = moda.accept()\n"
        "    print(type(x).__name__)\n"
        "    y = await x\n"
        "    print(y[0], y[2])\n"
        "def main() -> None:\n"
        "    asyncio.run(main_async())\n"
        "if __name__ == \"__main__\":\n"
        "    main()\n",
        encoding="utf-8",
    )
    exe = tmp_path / "prog.out"
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["coroutine", "1 3"]


def test_coroutine_cannot_be_awaited_twice(tmp_path):
    """CPython raises RuntimeError when the same coroutine object is
    awaited after it has already completed."""
    result = _compile_and_run(tmp_path, """
        import asyncio

        async def inner():
            return 5

        async def outer():
            c = inner()
            print(await c)
            try:
                print(await c)
            except RuntimeError:
                print("RuntimeError")

        def main() -> None:
            asyncio.run(outer())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["5", "RuntimeError"]


def test_await_rejects_plain_object(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        async def outer():
            try:
                await 1
            except TypeError:
                print("TypeError")

        def main() -> None:
            asyncio.run(outer())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TypeError"


def test_asyncio_sleep_zero(tmp_path):
    """``await asyncio.sleep(0)`` yields control back to the loop and
    resumes — minimal event-loop scheduling."""
    result = _compile_and_run(tmp_path, """
        import asyncio

        async def two_steps():
            await asyncio.sleep(0)
            return "done"

        def main() -> None:
            print(asyncio.run(two_steps()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"


# ---------------------------------------------------------------------------
# async iteration / async with
# ---------------------------------------------------------------------------


def test_async_for(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class AIter:
            def __init__(self):
                self.i = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self.i >= 3:
                    raise StopAsyncIteration
                v = self.i
                self.i = self.i + 1
                return v

        async def collect():
            out = []
            async for x in AIter():
                out.append(x)
            return out

        def main() -> None:
            for v in asyncio.run(collect()):
                print(v)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "1", "2"]


def test_async_for_else(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class AIter:
            def __init__(self):
                self.i = 0
            def __aiter__(self):
                return self
            async def __anext__(self):
                if self.i >= 2:
                    raise StopAsyncIteration
                v = self.i
                self.i = self.i + 1
                return v

        async def collect():
            async for x in AIter():
                print(x)
            else:
                print("else")

        def main() -> None:
            asyncio.run(collect())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "1", "else"]


def test_async_with(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class CM:
            async def __aenter__(self):
                print("enter")
                return self
            async def __aexit__(self, et, ev, tb):
                print("exit")
                return False

        async def go():
            async with CM():
                print("body")

        def main() -> None:
            asyncio.run(go())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["enter", "body", "exit"]


def test_async_with_suppresses_body_exception(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class CM:
            async def __aenter__(self):
                print("enter")
                return self
            async def __aexit__(self, et, ev, tb):
                print("exit")
                return True

        async def go():
            async with CM():
                print("body")
                raise ValueError("boom")
            print("after")

        def main() -> None:
            asyncio.run(go())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "enter", "body", "exit", "after",
    ]


# ---------------------------------------------------------------------------
# __await__ protocol on user objects
# ---------------------------------------------------------------------------


def test_user_awaitable(tmp_path):
    """A user-defined class with ``__await__`` returning an iterator
    can be awaited from a coroutine."""
    result = _compile_and_run(tmp_path, """
        import asyncio

        class Awaitable:
            def __await__(self):
                yield  # one suspend point
                return 7

        async def use():
            return await Awaitable()

        def main() -> None:
            print(asyncio.run(use()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7"


def test_user_awaitable_can_read_self(tmp_path):
    result = _compile_and_run(tmp_path, """
        import asyncio

        class Awaitable:
            def __init__(self, value):
                self.value = value
            def __await__(self):
                yield
                return self.value + 5

        async def use():
            return await Awaitable(7)

        def main() -> None:
            print(asyncio.run(use()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "12"
