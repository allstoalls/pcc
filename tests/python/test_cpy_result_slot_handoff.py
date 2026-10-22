"""CPython bridges publish proven PCC owners before managed-slot cleanup.

These are host lowering and owned-IR protocol checks, not native execution or
moving-GC qualification. Compatibility imports deliberately have no native
provider; strict no-libpython checks keep that route unavailable.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Name
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _emit(source, *, module_name="bridge_handoff", strict=False, codegen_type=L1CodeGen):
    module = infer_module(parse_and_lift(source, "bridge_handoff.py", module_name))
    codegen = codegen_type(module, emit_cpy_main_exitcode=False, ir_scaffold_mode="off")
    codegen._strict_no_libpython = strict
    text = str(codegen.generate(module))
    verify_ir_text(text)
    return text


def _body(text, suffix="probe"):
    match = re.search(r"^define [^\n]*@user_[^\n(]*_" + re.escape(suffix)
                      + r"\([^\n]*\) \{\n(.*?)^\}", text, re.M | re.S)
    assert match is not None
    return match.group(1)


def _bridge_publication(body):
    bridges = list(re.finditer(
        r"(%[^\s]+) = call ptr [^\n]*@py_cpy_to_pcc_obj\(ptr (%[^\s)]+)\)\n", body,
    ))
    assert len(bridges) == 1
    bridge = bridges[0]
    publication = re.match(r"\s*store ptr " + re.escape(bridge[1])
                           + r", ptr (%[^\s,]+)", body[bridge.end():])
    assert publication is not None, body[bridge.end():bridge.end() + 500]
    return bridge, publication[1]


@pytest.mark.parametrize("call", [
    "make()", "make(value)", "make(value, value, value)",
    "make(value, value, value, value)", "make(item=value)",
])
@pytest.mark.parametrize("consumer", ["callback(VALUE)", "callback(item=VALUE)"])
def test_cpython_call_result_is_published_into_argument_sink(tmp_path, call, consumer):
    source = ("from compatibility_provider import make\n"
              "def probe(callback, value):\n    return "
              + consumer.replace("VALUE", call) + "\n")
    text = _emit(source)
    (tmp_path / "probe.ll").write_text(text)
    body = _body(text)
    bridge, output = _bridge_publication(body)
    assert "@py_cpy_call" in body[:bridge.start()]
    assert "@py_obj_call_slots" in body[bridge.end():]
    # Consume only the owned CPython source. The PCC result is owned by the
    # output slot and must not be decref'd through CPython's object layout.
    assert re.search(r"@py_cpy_decref\(ptr " + re.escape(bridge[2]) + r"\)", body)
    assert not re.search(r"@py_cpy_decref\(ptr " + re.escape(bridge[1]) + r"\)", body)
    assert re.search(r"load ptr, ptr " + re.escape(output), body[bridge.end():])


def test_default_binding_uses_the_same_cpython_result_sink(tmp_path):
    # A tuple default's identity belongs to the function, so the omitted
    # default keeps this call with the signature binder.
    text = _emit("""from compatibility_provider import make
def consume(value, default=('unused',)):
    return value
def probe(value):
    return consume(make(value))
""")
    (tmp_path / "default.ll").write_text(text)
    body = _body(text)
    _bridge_publication(body)
    assert "@py_obj_call_slots" in body
    assert "@user_bridge_handoff_consume(" not in body


def test_original_classgen_factory_helper_reaches_managed_call(tmp_path):
    from pcc.frontends.python.codegen.class_gen import ClassLowering

    path = Path(inspect.getsourcefile(ClassLowering))
    source = path.read_text()
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.FunctionDef)
                and node.name == "_classgen_dataclass_factory_default")
    exact_helper = ast.get_source_segment(source, node)
    assert exact_helper is not None
    text = _emit(exact_helper, module_name="pcc.frontends.python.codegen.class_gen")
    (tmp_path / "original-helper.py").write_text(exact_helper)
    (tmp_path / "original-helper.ll").write_text(text)
    body = _body(text, "_classgen_dataclass_factory_default")
    _bridge_publication(body)
    assert "@py_obj_call_slots" in body


def test_bridge_publication_errors_consume_source_once(tmp_path):
    text = _emit("""from compatibility_provider import make
def probe(callback, value):
    return callback(make(value))
""")
    (tmp_path / "cleanup.ll").write_text(text)
    body = _body(text)
    bridge, output = _bridge_publication(body)
    # Traverse from the CPython call itself, including its NULL-result edge,
    # the bridge's NULL-result edge, publication lease failures, and all
    # later success/failure paths. Each consumes the CPython source once.
    blocks = dict(re.findall(r"^([^\s:]+):\n(.*?)(?=^[^\s:]+:\n|\Z)", body, re.M | re.S))
    producer = re.search(re.escape(bridge[2]) + r" = call ptr [^\n]*@py_cpy_call[^\n]*", body)
    assert producer is not None
    start = next(name for name, block in blocks.items() if producer[0] in block)
    first = blocks[start][blocks[start].index(producer[0]):]
    pending = [(start, first, (), 0)]
    paths = []
    while pending:
        name, block, seen, count = pending.pop()
        assert name not in seen
        count += len(re.findall(r"@py_cpy_decref\(ptr " + re.escape(bridge[2]) + r"\)", block))
        labels = re.findall(r"\blabel %([^,\s]+)", block)
        if labels:
            for label in labels:
                pending.append((label, blocks[label], seen + (name,), count))
        else:
            paths.append(count)
    assert paths and set(paths) == {1}, paths
    # Normal continuation reloads from the authoritative slot after decref.
    normal = next(block for block in blocks.values()
                  if re.search(r"@py_cpy_decref\(ptr " + re.escape(bridge[2]) + r"\)", block)
                  and "call.arg.bridge.result" in block)
    decref_at = normal.index("@py_cpy_decref")
    assert re.search(r"load ptr, ptr " + re.escape(output), normal[decref_at:])


def test_strict_no_libpython_rejects_unsupported_provider_without_a_bridge(tmp_path):
    text = _emit("""def probe(callback, value):
    from unavailable_compatibility_provider import make
    return callback(make(value))
""", strict=True)
    (tmp_path / "strict.ll").write_text(text)
    message = "No module named 'unavailable_compatibility_provider'"
    assert "".join("\\" + format(byte, "02X") for byte in message.encode()) in text
    assert "@py_raise" in _body(text)
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)


class _BridgeReturnCodegen(L1CodeGen):
    def _emit_call(self, expr):
        if isinstance(expr.func, Name) and expr.func.ident == "slot_bridge_probe":
            output = self._new_slot_call_root("bridge.return")
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cpy = self._cpy_operand_cleanup_block
            self._try_err_block = self._slot_call_cleanup_block((output,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                source = expr.args[0]
                value = self._emit_expr(source)
                assert value in self._cpy_values
                self._emit_value_as_pcc_object_or_bridge(
                    value, source.ty, "return.bridge", result_slot=output,
                )
                return self._take_slot_call_root(output)
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cpy
        return super()._emit_call(expr)


@pytest.mark.parametrize("borrowed", [False, True])
def test_shared_bridge_preserves_cpython_source_ownership(tmp_path, borrowed):
    source = ("from compatibility_provider import make\n"
              "def probe():\n    return slot_bridge_probe("
              + ("make" if borrowed else "make()") + ")\n")
    text = _emit(source, codegen_type=_BridgeReturnCodegen)
    (tmp_path / "return.ll").write_text(text)
    body = _body(text)
    bridge, _output = _bridge_publication(body)
    decrefs = re.findall(r"@py_cpy_decref\(ptr " + re.escape(bridge[2]) + r"\)", body)
    assert bool(decrefs) is (not borrowed)
    assert "@pcc_gc_take_pinned_slot" in body[bridge.end():]
