from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).absolute().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


def _read(rel: str) -> str:
    return (RUNTIME / rel).read_text(encoding="utf-8")


def _event_names(category: int) -> dict[int, str]:
    """The event code -> name table py_runtime_log.py prints for a category."""
    log_src = _read("py/py_runtime_log.py")
    body = log_src.split("def _event_from_code(category: int, event: int)", 1)[1]
    body = body.split(f"    if category == {category}:\n", 1)[1]
    body = body.split("\n    if category == ", 1)[0]
    names: dict[int, str] = {}
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("if event == ") and index + 1 < len(lines):
            code = int(stripped.removeprefix("if event == ").rstrip(":"))
            returned = lines[index + 1].strip()
            names[code] = returned.split('cstr("', 1)[1].split('")', 1)[0]
    return names


def test_refcount_log_events_are_in_pcc_python_dispatch_layers():
    py_src = _read("py/py_obj.py")
    for event_code in ("(3, 1", "(3, 2", "(3, 3"):
        assert f"pcc_diagnostics_runtime_log_event_code{event_code}" in py_src
    assert _event_names(3) == {1: "incref", 2: "decref", 3: "free"}


def test_weakref_log_events_are_in_pcc_python_layers():
    py_src = _read("py/py_weakref.py")
    for event_code in ("(4, 1", "(4, 2", "(4, 3", "(4, 4"):
        assert f"pcc_diagnostics_runtime_log_event_code{event_code}" in py_src
    assert _event_names(4) == {
        1: "new",
        2: "invalidate",
        3: "callback",
        4: "dealloc",
    }


def test_finalizer_log_events_are_in_pcc_python_layers():
    py_src = _read("py/py_dunder.py")
    for event_code in ("(5, 2", "(5, 3", "(5, 4"):
        assert f"pcc_diagnostics_runtime_log_event_code{event_code}" in py_src
    names = _event_names(5)
    assert (names[2], names[3], names[4]) == ("call", "done", "skipped")


def test_integer_coded_runtime_log_api_is_declared_for_pcc_python_ports():
    header = _read("src/py_internal.h")
    log_src = _read("py/py_runtime_log.py")
    assert "pcc_diagnostics_runtime_log_event_code" in header
    assert '@c_abi_export("pcc_diagnostics_runtime_log_event_code")' in log_src
    assert "def _category_from_code(category: int)" in log_src


def test_runtime_log_init_suppresses_reentrant_events_before_first_getenv():
    py_src = _read("py/py_runtime_log.py")

    py_init = py_src.split("def _init_once() -> None:", 1)[1]
    assert py_init.index('global_addr("pcc_diagnostics_runtime_log_fast_state")') < py_init.index(
        'pcc_platform_getenv(cstr("PCC_LOG"))'
    )
