"""TaskGroup semantics, separately from native coroutine lowering."""
from __future__ import annotations

import importlib
import inspect
import contextvars
import pytest


@pytest.fixture
def model(monkeypatch):
    module = importlib.import_module("pcc.py_stdlib.asyncio")
    monkeypatch.setattr(module, "_LOOP_BOX", [None])
    monkeypatch.setattr(module, "_RUNNING_LOOP_BOX", [None])
    monkeypatch.setattr(module, "_TASKS", [])
    monkeypatch.setattr(module, "_SERVERS", [])
    monkeypatch.setattr(module, "_ACTIVE_RELAYS", [])
    monkeypatch.setattr(module, "_is_none", lambda value: value is None)
    monkeypatch.setattr(module, "_py_await_iterator", lambda value: value if inspect.iscoroutine(value) else value.__await__())
    def resume(iterator, value, error):
        return iterator.throw(error) if error is not None else iterator.send(value)
    monkeypatch.setattr(module, "_py_await_step", resume)
    yield module
    assert module._TASKS == []


def test_group_overlaps_waits_and_keeps_dynamic_results(model):
    seen = []
    async def child(value):
        seen.append(("start", value))
        await model.sleep(0.01)
        assert len([entry for entry in seen if entry[0] == "start"]) == 3
        return value * 2
    async def main():
        function = {"child": child}["child"]
        async with model.TaskGroup() as group:
            tasks = [group.create_task(function(value)) for value in range(3)]
        return [task.result() for task in tasks]
    assert model.run(main()) == [0, 2, 4]


def test_group_failure_cancels_and_drains_siblings(model):
    cleaned = []
    async def sibling():
        try:
            await model.sleep(30)
        finally:
            cleaned.append("done")
    async def failing():
        await model.sleep(0)
        raise ValueError("boom")
    async def main():
        async with model.TaskGroup() as group:
            group.create_task(sibling())
            group.create_task(failing())
    with pytest.raises(ExceptionGroup) as captured:
        model.run(main())
    assert [str(error) for error in captured.value.exceptions] == ["boom"]
    assert cleaned == ["done"]


def test_external_cancellation_survives_group_exit(model):
    cleaned = []
    async def child():
        try:
            await model.sleep(30)
        finally:
            cleaned.append(True)
    async def parent():
        async with model.TaskGroup() as group:
            group.create_task(child())
            await model.sleep(30)
    async def main():
        task = model.create_task(parent())
        await model.sleep(0.01)
        task.cancel()
        with pytest.raises(model.CancelledError):
            await task
        assert task.cancelling() == 1
    model.run(main())
    assert cleaned == [True]


def test_tasks_copy_context_and_future_callbacks_are_deferred(model):
    variable = contextvars.ContextVar("task-value", default="outer")
    async def child(value):
        variable.set(value)
        await model.sleep(0)
        return variable.get()
    async def main():
        future = model.Future()
        called = []
        future.add_done_callback(lambda done: called.append(done.result()))
        future.set_result(7)
        assert called == []
        async with model.TaskGroup() as group:
            left = group.create_task(child("left"))
            right = group.create_task(child("right"))
        assert called == [7]
        assert variable.get() == "outer"
        return left.result(), right.result()
    assert model.run(main()) == ("left", "right")


def test_group_early_return_waits_and_rejects_reentry(model):
    seen = []
    group = model.TaskGroup()
    async def child():
        await model.sleep(0)
        seen.append(True)
    async def main():
        async with group:
            group.create_task(child())
            return 9
    assert model.run(main()) == 9
    assert seen == [True]
    with pytest.raises(RuntimeError):
        model.run(group.__aenter__())


def test_group_cancel_before_entry_is_consumed(model):
    group = model.TaskGroup()
    group.cancel()
    async def main():
        async with group:
            await model.sleep(0)
        return model.current_task().cancelling()
    assert model.run(main()) == 0
