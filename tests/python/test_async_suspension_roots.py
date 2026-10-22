"""Generated operand owners survive coroutine suspension and completion."""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.test_slot_call_operand_roots import _emit


@pytest.mark.parametrize("expression", (
    "slot_operand_probe(await child())",
    "slot_args_probe(['before'], await child())",
    "slot_operand_probe((['before'], await child()))",
    "slot_operand_probe({'before': ['kept'], 'after': await child()})",
))
def test_awaited_operand_has_persistent_roots_and_valid_resume_edges(expression):
    text = _emit(
        "async def child():\n    return 42\n"
        "async def probe():\n    return " + expression + "\n"
    )
    verify_parsed_module(parse_self_backend_module(text))
    body = re.search(r"^define [^\n]*@user_slot_operand_probe__gen_resume\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    body = body.group(0)
    # Suspension dispatch must restore generated argument owners as well as
    # source locals; a root only registered on the previous native stack is
    # insufficient. The yielded owner is transferred after local cleanup.
    dispatch = re.search(
        r"^gen\.dispatch:\n(.*?)(?=^[^ \n][^\n]*:\n|^})",
        body,
        re.M | re.S,
    )
    assert dispatch is not None
    restores = re.findall(r"^  %gen\.operand\.restore[^\n]* = call ptr[^\n]*", body, re.M)
    assert restores
    assert all(restore in dispatch.group(1) for restore in restores)
    assert dispatch.group(1).rfind("gen.operand.restore") < dispatch.group(1).index("switch i64")
    assert "@py_tls_exc_swap_slot(" in body
    handoff = re.search(r"(%gen.yield.owner[^ ]*) = call ptr[^\n]*@pcc_gc_take_pinned_slot[^\n]*\n  ret ptr \1", body)
    assert handoff is not None, "yield ownership must transfer after every cleanup call"
    step = re.search(r"(%[^ ]+) = call ptr[^\n]*@py_await_step[^\n]*\n([^\n]*)", body)
    assert step is not None
    assert "store ptr " + step.group(1) in step.group(2)


@pytest.mark.parametrize("bulk", ("0", "1"))
def test_generated_coroutine_output_slots_start_empty(monkeypatch, bulk):
    monkeypatch.setenv("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", bulk)
    text = _emit(
        "async def child():\n    return 42\n"
        "async def probe():\n    return slot_operand_probe(await child())\n"
    )
    verify_parsed_module(parse_self_backend_module(text))
    resume = re.search(r"^define [^\n]*@user_slot_operand_probe__gen_resume\([^\n]*\).*?^}", text, re.M | re.S)
    factory = re.search(r"^define [^\n]*@user_slot_operand_probe\([^\n]*\).*?^}", text, re.M | re.S)
    assert resume is not None and factory is not None
    restored = re.findall(r"%gen.operand.restore[^ ]* = call ptr", resume.group(0))
    empty = re.findall(r"call void[^\n]*@(?:py_list_append|py_gen_frame_set)\([^\n]*, ptr null\)", factory.group(0))
    assert len(empty) == len(restored) > 0


_ASYNC_SUSPENSION_NATIVE_PROGRAM = r"""import gc

class Pause:
    def __await__(self):
        yield 'pause'
        return {'value': 42}

def keep(*args):
    print(args[0][0], args[1]['value'])
    return args[1]

async def child():
    return await Pause()

async def parent():
    return keep(['held'], await child())

marker = ValueError('cancel')

async def cancelled():
    try:
        return keep(['cancelled'], await Pause())
    except ValueError as error:
        print('same', error is marker)
        return 17

class Manager:
    async def __aenter__(self):
        return 'entered'

    async def __aexit__(self, cls, error, traceback):
        print('exit', error is marker)
        await Pause()
        return False

async def managed():
    async with Manager() as value:
        print(value)
        await Pause()
        raise marker

events = []

class Token:
    def __del__(self):
        events.append('drop')

def second(*args):
    return args[1]

async def held():
    return second(Token(), await Pause())

def main():
    coro = parent()
    print(coro.send(None))
    try:
        coro.send(None)
    except StopIteration as error:
        print('done', error.value['value'])
    coro = cancelled()
    print(coro.send(None))
    try:
        coro.throw(marker)
    except StopIteration as error:
        print('cancelled', error.value)
    coro = managed()
    print(coro.send(None))
    print(coro.send(None))
    try:
        coro.send(None)
    except ValueError as error:
        print('propagated', error is marker)
    coro = held()
    print(coro.send(None))
    print('alive', len(events))
    coro.close()
    gc.collect()
    print('closed', len(events))

main()
"""


def _assert_native_async_program_matches_cpython(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
    program,
):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "await_lifetime.py"
    executable = tmp_path / "await_lifetime"
    source.write_text(program, encoding="utf-8")
    oracle = subprocess.run(
        [sys.executable, "-B", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert oracle.returncode == 0, oracle.stdout + oracle.stderr
    monkeypatch.setenv("PCC_HOST_PYTHON", sys.executable)
    compile_python(
        str(source),
        str(executable),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for requested_backend in range(5):
        log = tmp_path / ("gc" + str(requested_backend) + ".jsonl")
        ran = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            timeout=20,
            env=dict(
                os.environ,
                PCC_GC_BACKEND=str(requested_backend),
                PCC_LOG="gc",
                PCC_LOG_FORMAT="json",
                PCC_LOG_FILE=str(log),
            ),
        )
        assert ran.returncode == oracle.returncode, ran.stdout + ran.stderr
        assert ran.stdout == oracle.stdout
        assert ran.stderr == oracle.stderr
        assert log.is_file()
        observed_backends = {
            event.fields["value1"]
            for event in parse_log_lines(log.read_text(encoding="utf-8").splitlines())
            if event.fields.get("category") == "gc"
            and event.event in ("collect_start", "collect_stop", "collect_end")
        }
        assert observed_backends == {requested_backend}


_ASYNCIO_LIFECYCLE_PROGRAM = r"""import asyncio
import gc

events = []
marker = ValueError('identity')

async def worker():
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        events.append('cancel-cleanup')

class Manager:
    async def __aenter__(self):
        return 'entered'

    async def __aexit__(self, cls, error, traceback):
        print('exit-identity', error is marker)
        await asyncio.sleep(0)
        events.append('exit-cleanup')
        return False

async def main():
    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        print('cancelled', task.cancelled())
    print(events)
    try:
        async with Manager() as value:
            print(value)
            await asyncio.sleep(0)
            raise marker
    except ValueError as error:
        print('caught-identity', error is marker)
    gc.collect()
    print(events)

asyncio.run(main())
"""


@pytest.mark.parametrize("bulk", ("0", "1"))
def test_native_await_operands_preserve_exceptions_cleanup_and_lifetime(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
    bulk,
):
    monkeypatch.setenv("PCC_DISABLE_BULK_GENERATOR_FRAME_INIT", bulk)
    _assert_native_async_program_matches_cpython(
        tmp_path, monkeypatch, pcc_runtime_archive, _ASYNC_SUSPENSION_NATIVE_PROGRAM,
    )


def test_native_asyncio_cancellation_and_async_context_exception_identity(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    _assert_native_async_program_matches_cpython(
        tmp_path, monkeypatch, pcc_runtime_archive, _ASYNCIO_LIFECYCLE_PROGRAM,
    )
