"""Execute imported constructor owners with collection during argument cleanup."""
import os
import subprocess
import sys

import pytest


PROVIDER_SOURCE = '''import gc
from dataclasses import dataclass, field
class Box:
    def __init__(self, value, count: int):
        self.value = value
        self.count = count
class Temporary:
    def __init__(self, label, events, holder):
        self.label = label
        self.events = events
        self.holder = holder
    def __del__(self):
        self.events.append(self.label)
        self.holder[0] = Box("rebound", 3)
        gc.collect()
class Snapshot:
    def __init__(self, temporary, count: int):
        self.label = temporary.label
        self.count = count
        gc.collect()
class Raises:
    def __init__(self, temporary):
        gc.collect()
        raise ValueError("initializer")
@dataclass
class Defaults:
    values: list = field(default_factory=list)
class NewBox:
    def __new__(cls, value):
        return object.__new__(cls)
    def __init__(self, value):
        self.value = value
class Empty:
    pass
'''


ENTRY_SOURCE = '''import gc
import result_provider as provider
def take(*, value, later=None, **extras):
    gc.collect()
    return value
def mark(events, name, value):
    events.append(name)
    gc.collect()
    return value
def fail():
    gc.collect()
    raise ValueError("later")
def make_direct(events, holder):
    return provider.Snapshot(provider.Temporary("direct", events, holder), -(1 << 100))
def shadow(Box):
    return provider.Box("provider-owner", 17)
def main():
    events = []
    holder = [None]
    value = take(value=provider.Snapshot(provider.Temporary("nested", events, holder), 1 << 80))
    gc.collect()
    assert value.label == "nested" and value.count == 1 << 80
    assert events == ["nested"] and holder[0].value == "rebound"
    direct = make_direct(events, holder)
    gc.collect()
    assert direct.label == "direct" and direct.count == -(1 << 100)
    assert events == ["nested", "direct"]
    result = take(value=[provider.Box(provider.Box("inner", 7), 8)])
    assert result[0].value.value == "inner" and result[0].count == 8
    ordered = []
    value = take(value=provider.Box(count=mark(ordered, "count", 5), value=mark(ordered, "value", "data")))
    assert ordered == ["count", "value"] and value.value == "data" and value.count == 5
    first = take(value=provider.Defaults())
    second = take(value=provider.Defaults())
    first.values.append("first")
    assert first.values == ["first"] and second.values == []
    assert take(value=provider.NewBox("new-init")).value == "new-init"
    assert isinstance(shadow(None), provider.Box)
    try:
        take(value=provider.Raises(provider.Temporary("raising", events, holder)))
    except ValueError as error:
        assert str(error) == "initializer"
    else:
        raise AssertionError("missing initializer error")
    gc.collect()
    assert events[-1] == "raising" and holder[0].value == "rebound"
    try:
        take(value=provider.Box("later", 9), later=fail())
    except ValueError as error:
        assert str(error) == "later"
    else:
        raise AssertionError("missing later error")
    try:
        provider.Empty(1)
    except TypeError:
        pass
    else:
        raise AssertionError("missing constructor binding error")
    assert take(value=provider.Box("alive", 11)).count == 11
    print("IMPORTED_CONSTRUCTOR_RESULT_ROOTS_OK")
main()
'''


def _inputs(tmp_path):
    provider = tmp_path / "result_provider.py"
    entry = tmp_path / "entry.py"
    provider.write_text(PROVIDER_SOURCE)
    entry.write_text(ENTRY_SOURCE)
    return provider, entry


def test_imported_constructor_lifetime_reference(tmp_path):
    _provider, entry = _inputs(tmp_path)
    result = subprocess.run([sys.executable, "-B", str(entry)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout == "IMPORTED_CONSTRUCTOR_RESULT_ROOTS_OK\n"
    assert result.stderr == ""


@pytest.mark.integration
def test_imported_constructor_result_roots_native_five_gc(tmp_path, monkeypatch, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    provider, entry = _inputs(tmp_path)
    binary = tmp_path / "imported_constructor_result_roots"
    compile_python_multi(
        [str(provider), str(entry)], str(binary),
        module_names=["result_provider", "entry"], entry_module="entry",
        backend="self", libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        result = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)),
        )
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "IMPORTED_CONSTRUCTOR_RESULT_ROOTS_OK\n"
        assert result.stderr == ""
