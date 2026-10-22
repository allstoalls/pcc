"""Host executable compiles default to the direct indexed route."""
import os

import pytest

from pcc.frontends.python import pipeline_modes as modes

NAMES = modes.DIRECT_INDEXED_ROUTE_ENV
BASE = dict(backend=None, emit_llvm_only=False, python_library=False,
            target_triple=None, gpu_backend=None, libpython_mode=None)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in NAMES + ("PCC_PYTHON_IR_PASSES", "PCC_BACKEND", "PCC_GPU_BACKEND", "PCC_PYTHON_LIBPYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")


def test_host_executable_selects_and_restores_the_direct_route():
    chosen = modes.select_direct_indexed_route(**BASE)
    assert chosen == list(NAMES)
    assert all(os.environ[name] == "1" for name in NAMES)
    modes.restore_direct_indexed_route(chosen)
    assert not any(name in os.environ for name in NAMES)


@pytest.mark.parametrize("change", [
    {"emit_llvm_only": True}, {"python_library": True},
    {"target_triple": "x86_64-unknown-linux-gnu"}, {"gpu_backend": "metal"},
    {"libpython_mode": "on"}, {"libpython_mode": "auto"},
])
def test_text_outputs_and_other_modes_keep_the_text_route(change):
    assert modes.select_direct_indexed_route(**dict(BASE, **change)) == []
    assert not any(name in os.environ for name in NAMES)


def test_python_ir_passes_keep_the_text_route(monkeypatch):
    monkeypatch.delenv("PCC_PYTHON_IR_PASSES")
    assert modes.select_direct_indexed_route(**BASE) == []
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "mem2reg")
    assert modes.select_direct_indexed_route(**BASE) == []


@pytest.mark.parametrize("value", ["0", "1"])
def test_an_explicit_choice_wins(monkeypatch, value):
    monkeypatch.setenv(NAMES[0], value)
    assert modes.select_direct_indexed_route(**BASE) == []
    assert os.environ[NAMES[0]] == value and NAMES[1] not in os.environ


@pytest.mark.parametrize("value", ["0", "1"])
def test_an_explicit_inline_error_edge_choice_is_kept_with_the_route(monkeypatch, value):
    monkeypatch.setenv("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", value)
    chosen = modes.select_direct_indexed_route(**BASE)
    assert chosen == ["PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT"]
    assert os.environ["PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE"] == value
    modes.restore_direct_indexed_route(chosen)
    assert os.environ["PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE"] == value
