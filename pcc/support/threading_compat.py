"""Threading behavior compatibility scenarios for pcc.

The scenarios are intentionally tiny and deterministic.  They are used by tests
and by future CI scripts to compare pcc's native-threading shim with host
CPython behavior without relying on fixture-file spam.

Only scenarios that exercise CPython-portable surfaces are included. The
``threading.local`` shim in ``pcc/stdlib/threading.py`` exposes an explicit
``get`` / ``set`` / ``delete`` API which has no CPython counterpart, so it is
not tested via host parity here; see ``tests/test_threading_local.py`` for the
shim's own coverage.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThreadingScenario:
    name: str
    source: str
    expected_stdout: tuple[str, ...]


SCENARIOS: tuple[ThreadingScenario, ...] = (
    ThreadingScenario(
        name="thread-start-join",
        source=(
            "import threading\n"
            "def work():\n"
            "    print('worker')\n"
            "t = threading.Thread(target=work)\n"
            "print(t.is_alive())\n"
            "t.start()\n"
            "t.join()\n"
            "print(t.is_alive())\n"
        ),
        expected_stdout=("False", "worker", "False"),
    ),
    ThreadingScenario(
        name="lock-event",
        source=(
            "import threading\n"
            "lock = threading.Lock()\n"
            "print(lock.acquire())\n"
            "lock.release()\n"
            "ev = threading.Event()\n"
            "print(ev.is_set())\n"
            "ev.set()\n"
            "print(ev.is_set())\n"
        ),
        expected_stdout=("True", "False", "True"),
    ),
)


def scenario_names() -> tuple[str, ...]:
    return tuple(s.name for s in SCENARIOS)


def by_name(name: str) -> ThreadingScenario:
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise KeyError(name)
