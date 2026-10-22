"""Constructor cleanup must not erase a native field's runtime value domain."""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.py_ast import ClassDef, DynType, FuncDef, Return
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


_WRITES = (
    "self.provider = None\nself.provider = Provider()\nself.provider = None",
    "self.provider = Provider()\nif disabled:\n    self.provider = None",
    "self.provider = None\nif not disabled:\n    self.provider = Provider()",
    "self.provider = None\ntry:\n    self.provider = Provider()\nexcept Exception:\n    self.provider = None\n    raise",
    "self.provider = None\nself.provider = make_provider()",
)


def _schema_source(writes):
    return (
        "class Provider:\n    pass\n\n"
        "def make_provider():\n    return Provider()\n\n"
        "class Owner:\n"
        "    def __init__(self, disabled: bool = False):\n"
        + textwrap.indent(writes, "        ") + "\n"
        "    def get_provider(self):\n        return self.provider\n"
    )


@pytest.mark.parametrize("writes", _WRITES, ids=("cleanup", "value-first", "none-first", "exception-cleanup", "unknown-producer"))
def test_nullable_constructor_field_joins_all_writes(writes):
    source = _schema_source(writes)
    typed = infer_module(parse_and_lift(source, "nullable.py", "nullable"))
    owner = next(stmt for stmt in typed.body if isinstance(stmt, ClassDef) and stmt.name == "Owner")
    method = next(stmt for stmt in owner.body if isinstance(stmt, FuncDef) and stmt.name == "get_provider")
    result = next(stmt.value for stmt in method.body if isinstance(stmt, Return))
    assert isinstance(result.ty, DynType), result.ty


@pytest.mark.parametrize("writes", _WRITES, ids=("cleanup", "value-first", "none-first", "exception-cleanup", "unknown-producer"))
def test_exported_nullable_constructor_field_matches_local_schema(tmp_path, writes):
    path = tmp_path / "nullable.py"
    path.write_text(_schema_source(writes), encoding="utf-8")
    _, exports, _ = build_closed_world_context([str(path)], ["nullable"])
    owner = exports["nullable"]["Owner"]
    assert "provider" in owner["field_names"]
    assert dict(owner["field_types"]).get("provider", ("dyn",)) == ("dyn",)


def test_nullable_export_is_inherited_without_mutating_base_schema(tmp_path):
    source = """
class Provider:
    pass
class Base:
    def __init__(self):
        self.provider = Provider()
class OptionalOwner(Base):
    def __init__(self):
        self.provider = Provider()
        self.provider = None
class Child(OptionalOwner):
    pass
""".lstrip()
    path = tmp_path / "nullable.py"
    path.write_text(source, encoding="utf-8")
    _, exports, _ = build_closed_world_context([str(path)], ["nullable"])
    classes = exports["nullable"]
    assert dict(classes["Base"]["field_types"])["provider"][0] == "class"
    assert dict(classes["OptionalOwner"]["field_types"])["provider"] == ("dyn",)
    assert dict(classes["Child"]["field_types"])["provider"] == ("dyn",)


@pytest.mark.parametrize("writes, kind", (
    ("self.provider = None\nself.provider = None", "NoneType"),
    ("self.provider = Provider()\nself.provider = Provider()", "ClassType"),
))
def test_single_constructor_domain_keeps_its_type(writes, kind):
    source = _schema_source(writes)
    typed = infer_module(parse_and_lift(source, "nullable.py", "nullable"))
    owner = next(stmt for stmt in typed.body if isinstance(stmt, ClassDef) and stmt.name == "Owner")
    method = next(stmt for stmt in owner.body if isinstance(stmt, FuncDef) and stmt.name == "get_provider")
    result = next(stmt.value for stmt in method.body if isinstance(stmt, Return))
    assert type(result.ty).__name__ == kind


_PROVIDER_SOURCE = '''
class Channel:
    def __init__(self, identifier: int):
        self.identifier = identifier

    def close(self) -> None:
        print("closed", self.identifier)

class Provider:
    def new_channel(self, identifier: int):
        return Channel(identifier)
'''

_CONSUMER_SOURCE = '''
from pcc import virtual_thread
from nullable_provider import Provider

class Owner:
    def __init__(self, disabled: bool):
        self.provider = None
        if not disabled:
            self.provider = Provider()
            try:
                if disabled:
                    raise ValueError("construction failed")
            except Exception:
                self.provider = None
                raise

    def accept(self, identifier: int, fail: bool) -> None:
        channel = None
        try:
            if self.provider is not None:
                channel = self.provider.new_channel(identifier)
            if fail:
                raise ValueError("after creation")
        except Exception:
            if channel is not None:
                virtual_thread.call(channel.close)
            print("failed", identifier)
            return
        if channel is not None:
            virtual_thread.call(channel.close)
        else:
            print("absent", identifier)

def main() -> None:
    Owner(False).accept(1, False)
    Owner(False).accept(2, True)
    Owner(True).accept(3, False)
    Owner(True).accept(4, True)

main()
'''


def test_nullable_imported_factory_cleanup_runs_natively(tmp_path, monkeypatch, threaded_pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi

    # The threaded archive is admitted only for a threaded compilation.
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    provider = tmp_path / "nullable_provider.py"
    consumer = tmp_path / "nullable_consumer.py"
    provider.write_text(textwrap.dedent(_PROVIDER_SOURCE).lstrip(), encoding="utf-8")
    consumer.write_text(textwrap.dedent(_CONSUMER_SOURCE).lstrip(), encoding="utf-8")
    executable = tmp_path / "nullable_factory"
    compile_python_multi(
        [str(consumer), str(provider)], str(executable),
        module_names=["nullable_consumer", "nullable_provider"],
        entry_module="nullable_consumer", backend="self",
        libpython_mode="off", ir_scaffold_mode="on",
        runtime_archive=str(threaded_pcc_runtime_archive),
    )
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert run.stdout == "closed 1\nclosed 2\nfailed 2\nabsent 3\nfailed 4\n"
