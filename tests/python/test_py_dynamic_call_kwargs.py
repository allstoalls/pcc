import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def test_dynamic_call_merges_explicit_kwargs_and_starstar_for_codegen(tmp_path):
    src = tmp_path / "dynamic_call_mixed_kwargs.py"
    src.write_text(
        textwrap.dedent(
            """
            def target(**kwargs):
                return kwargs

            fn = target
            extra = {"b": 2}
            result = fn(a=1, **extra)
            print(result)
            """
        )
    , encoding="utf-8")
    ll = tmp_path / "dynamic_call_mixed_kwargs.ll"
    compile_python(
        str(src),
        str(ll),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )

    ir_text = ll.read_text(encoding="utf-8")
    assert "py_call_merge_kwargs" in ir_text


def test_dyn_list_method_runtime_guard_preserves_user_extend(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    src = tmp_path / "dynamic_list_method_guard.py"
    src.write_text(
        textwrap.dedent(
            """
            from typing import Any

            class Buffer:
                def __init__(self):
                    self.parts = b""

                def extend(self, data):
                    self.parts = self.parts + data

            def as_any(value: Any) -> Any:
                return value

            buf = as_any(Buffer())
            buf.extend(b"ab")
            print(buf.parts)

            xs = as_any([1])
            xs.extend([2, 3])
            print(len(xs), xs[2])
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "dynamic_list_method_guard"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["b'ab'", "3 3"]


def test_dyn_attr_extend_runtime_guard_preserves_user_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    src = tmp_path / "dynamic_attr_extend_guard.py"
    src.write_text(
        textwrap.dedent(
            """
            from typing import Any

            class Buffer:
                def __init__(self):
                    self.parts = b""

                def extend(self, data):
                    self.parts = self.parts + data

            def as_any(value: Any) -> Any:
                return value

            class Holder:
                def __init__(self):
                    self.buf = as_any(Buffer())
                    self.items = as_any([1])

                def add(self):
                    self.buf.extend(b"xy")
                    self.items.extend([2, 3])

            h = Holder()
            h.add()
            print(h.buf.parts)
            print(len(h.items), h.items[2])
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "dynamic_attr_extend_guard"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["b'xy'", "3 3"]


def test_dynamic_async_method_accepts_keyword_args_and_varkw(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    src = tmp_path / "dynamic_async_method_kwargs.py"
    src.write_text(
        textwrap.dedent(
            """
            from typing import Any
            import asyncio

            class Client:
                async def connect(self, reader_remote, writer_remote, rauth, host_name, port, **kw):
                    return host_name + ":" + str(port) + ":" + kw["sock"]

            def as_any(value: Any) -> Any:
                return value

            async def main():
                client = as_any(Client())
                result = await client.connect(
                    reader_remote=None,
                    writer_remote=None,
                    rauth=b"",
                    host_name="example.com",
                    port=80,
                    writer_cipher_r=None,
                    myhost="proxy",
                    sock="fd",
                )
                print(result)

            asyncio.get_event_loop().run_until_complete(main())
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "dynamic_async_method_kwargs"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "example.com:80:fd"


def test_dynamic_async_method_accepts_starstar_kwargs_unpack(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    src = tmp_path / "dynamic_async_method_starstar_kwargs.py"
    src.write_text(
        textwrap.dedent(
            """
            import asyncio

            class Proto:
                async def guess(self, reader, **kw):
                    return True

                async def accept(self, reader, user, writer, **kw):
                    print("accept", writer, kw.get("sock"))
                    return (user, "host", 80)

            async def run(protos, reader, **kw):
                for proto in protos:
                    user = await proto.guess(reader, **kw)
                    if user:
                        ret = await proto.accept(reader, user, **kw)
                        print("after", kw.get("writer"))
                        return ret

            async def main():
                kw = {"writer": "w", "sock": "fd"}
                ret = await run([Proto()], "r", **kw)
                print(ret)

            asyncio.get_event_loop().run_until_complete(main())
            """
        )
    , encoding="utf-8")
    exe = tmp_path / "dynamic_async_method_starstar_kwargs"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    result = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "accept w fd",
        "after w",
        "(True, 'host', 80)",
    ]
