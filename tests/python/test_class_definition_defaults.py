"""Class defaults are captured while the class body executes, not at calls."""

import contextlib
import io
import os
from pathlib import Path
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc.backend.owned_object_emit import emit_owned_object


PROGRAM = '''events = []
def mark(value):
    events.append(value)
    return object()

class Sample:
    sentinel = mark(1)
    original = sentinel
    def __init__(self, value=sentinel):
        self.value = value
    def method(self, value=mark(2)):
        return value
    alias = method
    @staticmethod
    def static(value=sentinel):
        return value
    @classmethod
    def class_method(cls, *, value=sentinel):
        return value
    sentinel = mark(3)

class NewOnly:
    sentinel = object()
    original = sentinel
    def __new__(cls, value=sentinel):
        return value
    sentinel = object()

class Private:
    __sentinel = object()
    original = __sentinel
    def method(self, value=__sentinel):
        return value
    __sentinel = object()

def probe():
    sample = Sample()
    print(sample.value is Sample.original)
    print(sample.method() is sample.method())
    print(sample.alias() is sample.method())
    print(Sample.static() is Sample.original)
    print(Sample.class_method() is Sample.original)
    print(Sample(value=Sample.sentinel).value is Sample.sentinel)
    print(NewOnly() is NewOnly.original)
    print(NewOnly(value=NewOnly.sentinel) is NewOnly.sentinel)
    print(Private().method() is Private.original)
    print(events)
probe()
'''
EXPECTED = "True\n" * 9 + "[1, 2, 3]\n"

PREPARED = '''events = []
def factory():
    events.append(1)
    return object()
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
class Sample(metaclass=Meta):
    sentinel = object()
    original = sentinel
    def method(self, value=factory()):
        return value
    def other(self, value=sentinel):
        return value
    sentinel = object()
def probe():
    sample = Sample()
    print(sample.method() is sample.method())
    print(sample.other() is Sample.original)
    print(events)
probe()
'''

FAILED_DEFAULTS = '''import gc
import weakref
references = []
finalized = []
class Produced:
    def __del__(self):
        finalized.append(1)
def first():
    value = Produced()
    references.append(weakref.ref(value))
    return value
def second():
    raise ValueError("second default")
def main():
    try:
        class Broken:
            def method(self, left=first(), right=second()):
                return left
    except ValueError as error:
        assert str(error) == "second default"
    gc.collect()
    assert len(references) == 1
    assert references[0]() is None
    assert finalized == [1]
    print("DEFAULT_OWNERS_RELEASED")
main()
'''



PREPARED_LOOKUPS = '''events = []
sentinel = object()
replacement = object()
fallback = object()
class Namespace:
    def __init__(self):
        self.data = {}
    def __setitem__(self, key, value):
        self.data[key] = value
    def __getitem__(self, key):
        if key == "sentinel":
            events.append("getitem")
            return replacement
        if key == "provided":
            return self.__missing__(key)
        if key == "fallback":
            events.append("fallback")
        if key == "bad":
            events.append("raise")
            raise ValueError("lookup boom")
        return self.data[key]
    def __missing__(self, key):
        events.append("missing")
        return replacement
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return Namespace()
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace.data)
class Sample(metaclass=Meta):
    sentinel = object()
    original = sentinel
    def method(self, value=sentinel):
        return value
    def missing(self, value=provided):
        return value
    def global_value(self, value=fallback):
        return value
try:
    class Broken(metaclass=Meta):
        def method(self, value=bad):
            return value
except ValueError as error:
    assert str(error) == "lookup boom"
else:
    raise AssertionError("lookup exception was swallowed")
def main():
    sample = Sample()
    assert sample.method() is replacement
    assert Sample.original is replacement
    assert sample.missing() is replacement
    assert sample.global_value() is fallback
    assert events == ["getitem", "getitem", "missing", "fallback", "raise"]
    print("PREPARED_LOOKUPS_OK")
main()
'''

PREPARED_MISSING = '''replacement = object()
class Namespace(dict):
    def __missing__(self, key):
        if key == "provided":
            return replacement
        raise KeyError(key)
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return Namespace()
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
class Sample(metaclass=Meta):
    def method(self, value=provided):
        return value
def main():
    assert Sample().method() is replacement
    print("PREPARED_MISSING_OK")
main()
'''

PREPARED_DICT_NAMES = '''marker = object()
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
class Sample(metaclass=Meta):
    def from_global(self, value=marker):
        return value
    def from_builtin(self, value=repr):
        return value
    alias = marker
try:
    class Broken(metaclass=Meta):
        def method(self, value=undefined_name):
            return value
except NameError as error:
    assert str(error) == "name 'undefined_name' is not defined", str(error)
else:
    raise AssertionError("missing name was not reported")
def main():
    sample = Sample()
    assert sample.from_global() is marker
    assert sample.from_builtin() is repr
    assert Sample.alias is marker
    print("PREPARED_DICT_NAMES_OK")
main()
'''

PREPARED_FAILED_DEFAULTS = '''class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
''' + FAILED_DEFAULTS.replace("class Broken:", "class Broken(metaclass=Meta):")

PREPARED_NAMESPACE_CONTROLS = [
    pytest.param(PREPARED_LOOKUPS, "PREPARED_LOOKUPS_OK\n", id="prepared-lookups"),
    pytest.param(PREPARED_MISSING, "PREPARED_MISSING_OK\n", id="prepared-missing"),
    pytest.param(PREPARED_DICT_NAMES, "PREPARED_DICT_NAMES_OK\n", id="prepared-dict-names"),
    pytest.param(PREPARED_FAILED_DEFAULTS, "DEFAULT_OWNERS_RELEASED\n", id="prepared-owner-unwind"),
]


@pytest.mark.parametrize("source_text,expected", PREPARED_NAMESPACE_CONTROLS)
def test_prepared_namespace_reference_and_ir(tmp_path, monkeypatch, source_text, expected):
    reference = io.StringIO()
    with contextlib.redirect_stdout(reference):
        exec(source_text, {})
    assert reference.getvalue() == expected
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "prepared_control.py"
    output = tmp_path / "prepared_control.ll"
    source.write_text(source_text)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    assert "class.definition.namespace.prepared" in text
    assert "namespace.body.lease" in text
    assert "namespace.body.release" in text


def _inherited_signature_program(frozen, factory):
    decorator = "@dataclass(frozen=True)" if frozen else "@dataclass"
    default = "field(default_factory=list)" if factory else "()"
    identity = "is not" if factory else "is"
    return f'''from dataclasses import dataclass, field
try:
    {decorator}
    class Base:
        first: object = {default}
        second: object = None
    {decorator}
    class Child(Base):
        third: object = ()
    {decorator}
    class Grandchild(Child):
        pass
except Exception:
    raise
def main():
    left = Grandchild()
    right = Grandchild()
    assert left.first {identity} right.first
    assert left.second is None
    assert left.third == ()
    print("INHERITED_SIGNATURE_ROOTS_OK")
main()
'''


INHERITED_SIGNATURE_PROGRAMS = [
    pytest.param(_inherited_signature_program(frozen, factory),
                 "INHERITED_SIGNATURE_ROOTS_OK\n",
                 id=("frozen" if frozen else "plain") +
                    ("-factory" if factory else "-default"))
    for frozen in (False, True)
    for factory in (False, True)
]


def _body(text, symbol):
    match = re.search(r"^define[^\n]*@" + re.escape(symbol) + r"\([^\n]*\)[^\n]*\{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None, symbol
    return match[1]


def test_class_defaults_reference_semantics():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAM, {})
    assert output.getvalue() == EXPECTED


def test_failed_default_factory_reference_releases_product():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(FAILED_DEFAULTS, {})
    assert output.getvalue() == "DEFAULT_OWNERS_RELEASED\n"


def test_class_defaults_ir_captures_factories_only_in_definition_order(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "class_defaults.py"
    output = tmp_path / "class_defaults.ll"
    source.write_text(PROGRAM)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    initializer = _body(text, "_pcc_py_module_init_class_defaults")
    probe = _body(text, "user_class_defaults_probe")
    factory_calls = list(re.finditer(r"call ptr[^\n]*@user_class_defaults_mark\(", initializer))
    assert len(factory_calls) == 3
    # Function objects are built by py_func_new_signature_slots since d0b8b6720.
    first_function = re.search(r"@py_func_new_(?:named|signature_slots)\(", initializer)
    assert first_function is not None
    assert factory_calls[0].start() < first_function.start() < factory_calls[1].start()
    assert factory_calls[1].start() < factory_calls[2].start()
    assert "@user_class_defaults_mark(" not in probe
    assert "@py_obj_load_method" in probe
    # Keyword constructors use the rooted callable/args/kwargs/result ABI.
    # Keep the loaded positional __new__ checks below, and verify the slots
    # that replaced the former py_obj_call_method_kwargs call site.
    constructors = re.findall(
        r"%ctor\.unpack\.call[^ \n]* = call i64 \(ptr, ptr, ptr, ptr\) "
        r"@py_obj_call_slots\(ptr ([^,]+), ptr ([^,]+), ptr ([^,]+), ptr ([^)]+)\)",
        probe,
    )
    # Constructors that need the runtime binder use the rooted slots ABI. The
    # fully supplied keyword constructor binds statically and calls __init__
    # directly. None reaches the unrooted py_obj_call_method_kwargs.
    assert len(constructors) == 3
    assert "@py_obj_call_method_kwargs(" not in probe
    assert re.search(r"@user_class_defaults_Sample___init__\(ptr %inst\.Sample", probe)
    aliases = dict(re.findall(r"(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr", probe))

    def original(value):
        while value in aliases:
            value = aliases[value]
        return value

    for operands in constructors:
        for role, operand in zip(("class", "args", "kwargs"), operands[:3]):
            assert re.match(r"%ctor\.unpack\." + role + r"\.operand\.", original(operand)), (role, operand)
        # Private().method() publishes directly into the rooted receiver sink.
        assert re.match(
            r"%(?:ctor\.unpack\.result|Private\.method\.bound\.callable\.receiver)\.operand\.",
            original(operands[3]),
        ), operands[3]
    # Only NewOnly(value=...) still binds keywords at run time.
    assert len(re.findall(r"call ptr[^\n]*@py_call_merge_kwargs_for_call\(", probe)) == 1
    assert "@pcc_gc_root_copy_lease" in probe
    assert "@py_obj_call_method(" in probe


def test_prepared_namespace_reuses_method_signatures(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "prepared_defaults.py"
    output = tmp_path / "prepared_defaults.ll"
    source.write_text(PREPARED)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    for symbol in ("main", "_pcc_py_module_init_prepared_defaults"):
        body = _body(text, symbol)
        assert len(re.findall(r"call ptr[^\n]*@user_prepared_defaults_factory\(", body)) == 1
        # Each namespace method is constructed once through the rooted callable
        # ABI (label ``namespace.method``), never through a signature wrapper.
        constructions = re.findall(
            r"%namespace\.method\.construction\.status[\w.]* = call i64[^\n]*@py_func_new_signature_slots\(",
            body,
        )
        assert len(constructions) == 2
        assert "signature.wrapper" not in body


def test_fully_supplied_defaults_keep_direct_static_abi(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "direct_defaults.py"
    output = tmp_path / "direct_defaults.ll"
    source.write_text('''from pcc import i64
class Builder:
    def publish(self, source, condition, error, line: i64, cleanup: i64, payload: i64 = -1):
        return payload
    def opaque(self, value=object()):
        return value
def direct(builder: Builder, value):
    Builder.publish(builder, "source", value, "error", i64(1), i64(2), i64(3))
    builder.opaque(value)
def captured(builder: Builder):
    return builder.opaque()
''')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    direct = _body(text, "user_direct_defaults_direct")
    assert "@user_direct_defaults_Builder_publish" in direct
    assert "@user_direct_defaults_Builder_opaque" in direct
    assert "@py_obj_load_method" not in direct
    # An omitted definition default comes from the live callable's captured
    # signature (loaded method or rooted bound-callable invoke), never from a
    # direct call that would re-evaluate ``object()`` at the call site.
    captured = _body(text, "user_direct_defaults_captured")
    assert "@user_direct_defaults_Builder_opaque" not in captured
    assert "@py_obj_load_method" in captured or re.search(
        r"%Builder\.opaque\.bound\.invoke[^ ]* = call i64 \(ptr, ptr, ptr, ptr\) @py_obj_call_slots\(",
        captured,
    )


@pytest.mark.parametrize("source_text", [PROGRAM, PREPARED], ids=["plain", "prepared"])
def test_definition_default_leases_reach_owned_object_emitter(tmp_path, monkeypatch, source_text):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "lease_joins.py"
    output = tmp_path / "lease_joins.ll"
    source.write_text(source_text)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    object_bytes = emit_owned_object(output.read_text(), "arm64-apple-darwin23.6.0")
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("prepared", [False, True], ids=["plain-raise", "prepared-raise"])
def test_raising_default_factory_unwinds_every_lease_before_join(tmp_path, monkeypatch, prepared):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "raising_defaults.py"
    output = tmp_path / "raising_defaults.ll"
    meta = '''class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
''' if prepared else ""
    header = "class Broken(metaclass=Meta):" if prepared else "class Broken:"
    source.write_text('''def fail():
    raise ValueError("default boom")
''' + meta + '''try:
    ''' + header + '''
        sentinel = object()
        def existing(self, value=sentinel):
            return value
        def failing(self, value=fail()):
            return value
except ValueError:
    print("DEFAULT_FAILED")
''')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    ir_text = output.read_text()
    # The partial signature tuples and the class body unwind before the
    # handler joins. Only a prepared namespace still pins its method objects;
    # ordinary methods are built under the rooted callable ABI.
    assert re.search(r"signature(?:\.tuples)?\.unwind", ir_text)
    if prepared:
        assert "pin.unwind" in ir_text
    assert "class.body.unwind" in ir_text
    object_bytes = emit_owned_object(ir_text, "arm64-apple-darwin23.6.0")
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"


def test_failed_signature_releases_partial_default_owners_at_emitter(tmp_path, monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "failed_default_owners.py"
    output = tmp_path / "failed_default_owners.ll"
    source.write_text(FAILED_DEFAULTS)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="arm64-apple-darwin23.6.0")
    ir_text = output.read_text()
    assert "func.signature.tuples.unwind" in ir_text
    object_bytes = emit_owned_object(ir_text, "arm64-apple-darwin23.6.0")
    assert object_bytes[:4] == b"\xcf\xfa\xed\xfe"


@pytest.mark.parametrize("source_text,expected", INHERITED_SIGNATURE_PROGRAMS)
def test_inherited_signature_null_guard_retires_nested_roots(tmp_path, monkeypatch, source_text, expected):
    reference = io.StringIO()
    with contextlib.redirect_stdout(reference):
        exec(source_text, {})
    assert reference.getvalue() == expected
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "inherited_signatures.py"
    output = tmp_path / "inherited_signatures.ll"
    source.write_text(source_text)
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    ir_text = output.read_text()
    initializer = _body(ir_text, "_pcc_py_module_init_inherited_signatures")
    # Each inherited default is copied from the base declaration into a rooted
    # slot. That copy's error edge must retire both inherited-metadata roots,
    # declaration (entered last) first, before the signature unwind. A
    # try-only cleanup override used to bypass this cleanup entirely.
    blocks = dict(re.findall(r"^([\w.$]+):\n(.*?)(?=^[\w.$]+:|\Z)", "entry:\n" + initializer, re.M | re.S))
    error_edges = re.findall(
        r"= call i64 \(ptr, i64, ptr\) @py_func_copy_default_slots\([^\n]*\n[^\n]*\n"
        r"  br i1 [^\n]*label %([\w.$]+),", initializer,
    )
    assert len(error_edges) >= 5  # two Child defaults, three Grandchild defaults
    for start in error_edges:
        leaves, seen, name = [], set(), start
        while name and name not in seen and not name.startswith("func.signature.tuples.unwind."):
            seen.add(name)
            body = blocks.get(name, "")
            leaves += re.findall(
                r"@pcc_gc_frame_leave_lifo\(ptr %(dataclass\.base(?:\.declaration)?)\.operand", body,
            )
            successors = re.findall(r"br label %([\w.$]+)", body)
            name = successors[-1] if successors else None
        assert name is not None and name.startswith("func.signature.tuples.unwind."), (start, name)
        assert leaves == ["dataclass.base.declaration", "dataclass.base"], (start, leaves)
    object_bytes = emit_owned_object(ir_text, "x86_64-unknown-linux-gnu")
    assert object_bytes[:4] == b"\x7fELF"


def test_inherited_signature_roots_reach_direct_object_worker(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline, pipeline_frontend_workers as workers

    for name in (
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES",
        "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK",
        "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND",
        "PCC_DIRECT_INDEXED_NATIVE_OBJECT",
    ):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_SIDECAR", "0")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "inherited_signatures.py"
    source.write_text(_inherited_signature_program(True, False))
    manifest = tmp_path / "worker.manifest"
    result = tmp_path / "result.tsv"
    workers.write_worker_manifest(
        str(manifest), str(result), str(tmp_path), "", "", [str(source)],
        ["inherited_signatures"], [0], entry_module="inherited_signatures",
        sibling_inits=(), libpython_mode="off", ir_scaffold_mode="on", verbose=False,
    )
    assert pipeline.run_python_multi_codegen_worker(str(manifest)) == 0, result.read_text()
    object_path = tmp_path / "module_0.direct.pco"
    assert object_path.stat().st_size > 0
    assert "\tPCO\t" + str(object_path) in result.read_text()


@pytest.mark.integration
@pytest.mark.parametrize("source_text,expected", [
    pytest.param(PROGRAM, EXPECTED, id="plain"),
    pytest.param(PREPARED, "True\nTrue\n[1]\n", id="prepared"),
    pytest.param(FAILED_DEFAULTS, "DEFAULT_OWNERS_RELEASED\n", id="factory-owner-unwind"),
    *INHERITED_SIGNATURE_PROGRAMS,
    *PREPARED_NAMESPACE_CONTROLS,
])
def test_native_class_definition_defaults(tmp_path, pcc_runtime_archive, python_program_compiler, source_text, expected):
    source = tmp_path / "class_defaults.py"
    binary = tmp_path / "class_defaults"
    source.write_text(source_text)
    python_program_compiler(str(source), str(binary), backend="self",
                            libpython_mode="off", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == expected, (backend, result.stdout, result.stderr)
