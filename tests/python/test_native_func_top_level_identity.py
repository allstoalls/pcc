"""Exercise native callable top-level classification in the real emitter."""

import builtins
from dataclasses import replace
import gc
import hashlib
import inspect
import json
import sys
import textwrap
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.xdist_group(name="native_func_top_level_identity")

_IDENTITY_FIRST = "if top_stmt is fd and isinstance(top_stmt, FuncDef):"
_TYPE_FIRST = "if isinstance(top_stmt, FuncDef) and top_stmt is fd:"


@pytest.fixture(autouse=True)
def _stable_codegen_environment(monkeypatch):
    for name in (
        "PCC_PY_DEBUG_INFO", "PCC_DEBUG_CODEGEN_PHASES",
        "PCC_DEBUG_BOOTSTRAP_TRACE", "PCC_PY_FRONTEND_WORKER_TIMING",
        "PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "PCC_DIRECT_INDEXED_KERNEL_EMIT",
        "PCC_TEXT_INDEXED_KERNEL_EMIT", "PCC_DIRECT_INDEXED_KERNEL_VALIDATE",
        "PCC_LOW_IR",
    ):
        monkeypatch.delenv(name, raising=False)


def _production_method():
    from pcc.frontends.python.codegen.user_function_lowering import (
        UserFunctionLoweringMixin,
    )

    return UserFunctionLoweringMixin._emit_native_func_value


def _type_first_control(method):
    """Recompile the real method with only its former predicate order."""
    source = textwrap.dedent(inspect.getsource(method))
    assert source.count(_IDENTITY_FIRST) == 1
    assert _TYPE_FIRST not in source
    namespace = {}
    exec(
        compile(
            source.replace(_IDENTITY_FIRST, _TYPE_FIRST),
            "<native-func-type-first-control>",
            "exec",
        ),
        method.__globals__,
        namespace,
    )
    return namespace[method.__name__]


def _function(name="target", value=7, args=()):
    from pcc.frontends.python.py_ast import (
        FuncDef, IntLit, IntType, Return, SourceSpan,
    )

    span = SourceSpan("native_func_identity.py", value, 1, value, 20)
    integer = IntType("int")
    return FuncDef(
        span, name, args, integer, (Return(span, IntLit(span, integer, value)),),
        has_return_annotation=True,
    )


class _ClassificationDone(Exception):
    pass


def _probe_classification(
    monkeypatch, method, fd, body, *, free_names=(), hoisted=False,
    fallback=False, duplicate=False,
):
    """Stop the actual emitter immediately after its cache classification."""
    from pcc.frontends.python.py_ast import FuncDef
    from pcc.ir.compat import ir

    module = ir.Module(name="identity_probe")
    function_type = ir.FunctionType(ir.VoidType(), [])
    full_fn = ir.Function(module, function_type, name="user_identity_probe_target")
    current = ir.Function(module, function_type, name="probe")
    builder = ir.IRBuilder(current.append_basic_block("entry"))
    counter = [0]
    checks = []
    fallback_calls = []

    def fresh(label):
        counter[0] += 1
        return label + "." + str(counter[0])

    def resolve(name):
        fallback_calls.append(name)
        return fd

    def stop(*_args):
        raise _ClassificationDone

    def counted_isinstance(value, cls):
        if cls is FuncDef and sys._getframe(1).f_code is method.__code__:
            checks.append(value)
        return builtins.isinstance(value, cls)

    host = SimpleNamespace(
        _native_symbol_funcdefs={} if fallback else {full_fn.name: fd},
        _find_user_funcdef=resolve,
        ast_module=SimpleNamespace(name="identity_probe", body=tuple(body)),
        _hoisted_capture_params={"target": ()} if hoisted else {},
        _duplicate_module_function_names={"target"} if duplicate else set(),
        module=module, builder=builder, current_function=current,
        _module_symbol_suffix=lambda name: name,
        _fresh=fresh, _emit_native_func_adapter=stop,
        runtime={
            "py_incref": ir.Function(
                module,
                ir.FunctionType(ir.VoidType(), [ir.IntType(8).as_pointer()]),
                name="py_incref",
            ),
        },
    )
    with monkeypatch.context() as patch:
        patch.setitem(method.__globals__, "isinstance", counted_isinstance)
        with pytest.raises(_ClassificationDone):
            method(host, "target", "target", full_fn, free_names)
    caches = tuple(
        name for name in module.globals
        if name.startswith("__pcc_native_func_value_cache_")
    )
    return len(checks), caches, tuple(fallback_calls)


def test_real_emitter_identity_counts_and_existing_gates(monkeypatch, record_property):
    method = _production_method()
    fd = _function()
    other = replace(fd)
    assert other == fd and other is not fd
    foreign = type("FuncDef", (), {})()
    foreign.name, foreign.args, foreign.return_ty = "target", (), fd.return_ty

    class AbsentCustomDefinition:
        args = ()
        return_ty = fd.return_ty

        @property
        def __class__(self):
            raise AssertionError("absent resolver result must not be type-inspected")

    cases = (
        ("late", fd, (object(), other, fd), {}, 1, True, ()),
        ("equal-not-identical", fd, (other,), {}, 0, False, ()),
        ("absent", fd, (), {}, 0, False, ()),
        ("fallback", fd, (other, fd), {"fallback": True}, 1, True, ("target",)),
        ("free-names", fd, (fd,), {"free_names": ("capture",)}, 0, False, ()),
        ("hoisted", fd, (fd,), {"hoisted": True}, 0, False, ()),
        ("foreign-kind", foreign, (foreign,), {}, 1, False, ()),
        ("absent-custom", AbsentCustomDefinition(), (other,), {}, 0, False, ()),
        ("duplicate", fd, (other, fd), {"duplicate": True}, 1, True, ()),
    )
    observed = {}
    for label, resolved, body, options, count, cached, fallback_calls in cases:
        result = _probe_classification(monkeypatch, method, resolved, body, **options)
        checks, caches, actual_fallback = result
        assert checks == count, label
        assert bool(caches) is cached, label
        assert actual_fallback == fallback_calls, label
        if label == "duplicate":
            assert caches == (
                "__pcc_native_func_value_cache_identity_probe_target."
                "user_identity_probe_target",
            )
        observed[label] = checks
    record_property("identity_class_checks", json.dumps(observed, sort_keys=True))


def test_real_type_first_control_counts_same_identity_traversal(monkeypatch, record_property):
    method = _production_method()
    control = _type_first_control(method)
    fd = _function()
    body = (object(), replace(fd), fd)
    current = _probe_classification(monkeypatch, method, fd, body)
    previous = _probe_classification(monkeypatch, control, fd, body)
    assert current[0] == 1
    assert previous[0] == len(body)
    assert current[1:] == previous[1:]
    record_property("candidate_class_checks", current[0])
    record_property("type_first_class_checks", previous[0])


def _ir_fixture(kind):
    from pcc.frontends.python.py_ast import (
        Arg, Assign, AugAssign, BoolLit, BoolType, Call, DynType, Global, If,
        IntLit, IntType, Module, Name, Return, SourceSpan,
    )

    span = SourceSpan("native_func_identity.py", 1, 1, 1, 20)
    integer = IntType("int")
    dynamic = DynType("dyn")
    counter = Name(span, integer, "counter")
    initial = Assign(span, (counter,), IntLit(span, integer, 0), integer)
    if kind == "many-definitions":
        body = [initial]
        for index in range(6):
            body.append(_function("work_" + str(index), index + 2))
            body.append(Assign(
                span, (Name(span, integer, "value_" + str(index)),),
                IntLit(span, integer, index), integer,
            ))
    elif kind == "defaults-duplicates":
        make_default = replace(
            _function("make_default"),
            body=(
                Global(span, ("counter",)),
                AugAssign(span, counter, "+", IntLit(span, integer, 1)),
                Return(span, counter),
            ),
        )
        default = Call(
            span, integer, Name(span, dynamic, "make_default"), (), (),
        )
        arg = Arg("value", integer, default, "pos", has_default=True)
        first = _function("same", 11, (arg,))
        second = _function("same", 12, (arg,))
        body = [
            initial, make_default, first,
            Assign(
                span, (Name(span, dynamic, "escaped"),),
                Name(span, dynamic, "same"), dynamic,
            ),
            second,
        ]
    else:
        assert kind == "conditional-definition"
        body = [
            initial,
            If(span, BoolLit(span, BoolType("bool"), True), (_function(),), ()),
        ]
    return Module("identity_fixture", tuple(body))


def _generate_ir(monkeypatch, method, kind, debug, route):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen

    calls = []

    def counted_emitter(self, orig_name, resolved_name, full_fn, free_names):
        calls.append((resolved_name, full_fn.name))
        return method(self, orig_name, resolved_name, full_fn, free_names)

    with monkeypatch.context() as patch:
        patch.setenv("PCC_PY_DEBUG_INFO", "1" if debug else "")
        patch.setattr(L1CodeGen, "_emit_native_func_value", counted_emitter)
        codegen = L1CodeGen(_ir_fixture(kind))
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._skip_program_main = True
        codegen._native_module_exports = {}
        codegen._module_source_path = "native_func_identity.py"
        if route == "public":
            output = codegen.generate(codegen.ast_module)
        else:
            assert route == "worker-first-use"
            output = codegen._generate_initial_module()
    assert calls
    names = [name for name, _symbol in calls]
    if kind == "many-definitions":
        for index in range(6):
            name = "work_" + str(index)
            assert name in names
            assert "__pcc_native_func_value_cache_identity_fixture_" + name in codegen.module.globals
    elif kind == "defaults-duplicates":
        assert names.count("same") >= 2
        assert "make_default" in names
    else:
        assert "target" in names
        assert "__pcc_native_func_value_cache_identity_fixture_target" not in codegen.module.globals
    assert "define " in output
    assert ("DICompileUnit" in output) is debug
    if kind == "defaults-duplicates":
        assert "user_identity_fixture_same.definition.0" in output
        assert "user_identity_fixture_same.definition.1" in output
        assert "user_identity_fixture_make_default" in output
        assert "escaped" in output
    return output, tuple(calls)


@pytest.mark.parametrize("route", ["public", "worker-first-use"])
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize("kind", [
    "many-definitions", "defaults-duplicates", "conditional-definition",
])
def test_real_codegen_ir_matches_type_first_control(
    monkeypatch, record_property, tmp_path, kind, debug, route,
):
    method = _production_method()
    control = _type_first_control(method)
    current, calls = _generate_ir(monkeypatch, method, kind, debug, route)
    previous, control_calls = _generate_ir(monkeypatch, control, kind, debug, route)
    assert calls == control_calls
    assert current == previous
    (tmp_path / "candidate.ll").write_text(current)
    (tmp_path / "type-first-control.ll").write_text(previous)
    record_property("ir_sha256", hashlib.sha256(current.encode()).hexdigest())
    record_property("ir_bytes", len(current.encode()))
    record_property("callable_materializations", json.dumps(calls))
    gc.collect()
