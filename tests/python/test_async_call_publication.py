"""Ordinary async calls hand coroutine ownership to registered output slots."""
from __future__ import annotations

import pytest

from tests.python.test_slot_call_operand_roots import _emit, _probe_function


@pytest.mark.parametrize("source", (
    "async def value():\n    return 42\n"
    "def probe():\n    return slot_operand_probe(value())\n",
    "async def value() -> int:\n    return 42\n"
    "def probe():\n    return slot_operand_probe(value())\n",
    "class Worker:\n    async def value(self):\n        return 42\n"
    "def probe(worker: Worker):\n    return slot_operand_probe(worker.value())\n",
    "class Worker:\n    @staticmethod\n    async def value():\n        return 42\n"
    "def probe():\n    return slot_operand_probe(Worker.value())\n",
    "class Worker:\n    @classmethod\n    async def value(cls):\n        return 42\n"
    "def probe():\n    return slot_operand_probe(Worker.value())\n",
))
def test_async_coroutine_construction_uses_owned_output(source):
    body = _probe_function(_emit(source))
    assert "@py_obj_call_slots(" in body
    assert "@py_coroutine_new_resumable(" not in body
    assert "@py_cpy_" not in body
