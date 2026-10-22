"""A method owns its local object-domain metadata and restores its caller."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.class_gen import ClassLowering
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.owned_ir_validation import verify_ir_text


def _codegen(source):
    module = infer_module(parse_and_lift(source, "method_domains.py", "method_domains"))
    return module, L1CodeGen(module, ir_scaffold_mode="on")


def _body(text, method):
    match = re.search(
        r"(?ms)^define[^\n]*@user_method_domains_Probe_" + method
        + r"\([^\n]*\).*?^}", text,
    )
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize("decorator", ("", "    @staticmethod\n", "    @classmethod\n"))
def test_prior_method_foreign_local_does_not_taint_native_parameter(decorator):
    prefix = "" if "staticmethod" in decorator else ("cls, " if "classmethod" in decorator else "self, ")
    module, codegen = _codegen(
        "from decimal import Decimal\n"
        "def consume(*, value, **extras):\n    return value\n"
        "class Probe:\n"
        "    def read_foreign(self):\n"
        "        signature = Decimal('1')\n"
        "        return signature\n"
        + decorator
        + "    def write_native(" + prefix + "signature):\n"
        "        return consume(value=signature)\n"
    )
    text = str(codegen.generate(module))
    native = _body(text, "write_native")
    foreign = _body(text, "read_foreign")
    assert "@py_cpy_call" in foreign
    assert "@py_cpy_" not in native
    assert "@pcc_gc_root_copy_borrowed_lease(" in native
    verify_ir_text(text)


def test_method_foreign_capture_keeps_bridge_and_native_cell_owner():
    module, codegen = _codegen(
        "from decimal import Decimal\n"
        "class Probe:\n"
        "    def capture_foreign(self):\n"
        "        existing = Decimal('1')\n"
        "        return lambda value: existing\n"
    )
    text = str(codegen.generate(module))
    body = _body(text, "capture_foreign")
    bridge = re.search(
        r"(?P<result>%[^ ]+) = call [^\n]*@py_cpy_to_pcc_obj\(ptr (?P<source>%[^)]+)\)",
        body,
    )
    assert bridge is not None
    source = bridge.group("source")
    # Hoisting stores the foreign local in a native closure cell. The NEW
    # foreign result is bridged and disposed; the closure then captures
    # that managed cell through its own authoritative slot.
    assert re.search(r"@py_cpy_decref\(ptr " + re.escape(source) + r"\)", body)
    assert "lambda.native.capture.operand" in body
    assert "@pcc_gc_root_copy_lease(" in body
    verify_ir_text(text)


@pytest.mark.parametrize("fail", (False, True))
def test_method_restores_enclosing_foreign_metadata(monkeypatch, fail):
    module, codegen = _codegen("class Probe:\n    def target(self):\n        return 1\n")
    outer_flags = {"outer": True}
    outer_values = {object()}
    outer_owned = {object()}
    # Observe at actual method entry so module-generation setup cannot erase
    # the enclosing state supplied to the scope under test.
    original = ClassLowering._emit_method_body
    observed = []
    class StopMethod(Exception):
        pass
    def emit_method(self, *args):
        self.parent._cpy_env_flags = outer_flags
        self.parent._cpy_values = outer_values
        self.parent._owned_cpy_values = outer_owned
        try:
            return original(self, *args)
        finally:
            assert self.parent._cpy_env_flags is outer_flags
            assert self.parent._cpy_values is outer_values
            assert self.parent._owned_cpy_values is outer_owned
            observed.append(True)
    original_stmts = L1CodeGen._emit_stmts
    def emit_stmts(self, stmts):
        if getattr(self.current_func_def, "name", None) == "target":
            assert not self._cpy_env_flags
            assert not self._cpy_values
            assert not self._owned_cpy_values
            if fail:
                raise StopMethod()
        return original_stmts(self, stmts)
    monkeypatch.setattr(ClassLowering, "_emit_method_body", emit_method)
    monkeypatch.setattr(L1CodeGen, "_emit_stmts", emit_stmts)
    if fail:
        with pytest.raises(StopMethod):
            codegen.generate(module)
    else:
        codegen.generate(module)
    assert observed
    assert outer_flags == {"outer": True}


def test_generator_method_does_not_inherit_prior_foreign_local():
    module, codegen = _codegen(
        "from decimal import Decimal\n"
        "def consume(*, value, **extras):\n    return value\n"
        "class Probe:\n"
        "    def read_foreign(self):\n"
        "        signature = Decimal('1')\n"
        "        return signature\n"
        "    def iter_native(self, signature):\n"
        "        yield consume(value=signature)\n"
    )
    text = str(codegen.generate(module))
    assert "iter_native" in text
    verify_ir_text(text)


def test_method_preserves_foreign_module_global_domain():
    module, codegen = _codegen(
        "from decimal import Decimal as foreign_global\n"
        "class Probe:\n"
        "    def read_foreign_global(self):\n"
        "        return foreign_global('1')\n"
    )
    text = str(codegen.generate(module))
    body = _body(text, "read_foreign_global")
    assert "@py_cpy_call" in body
    assert "foreign_global" in body
    assert "@py_obj_call_method" not in body
    verify_ir_text(text)
