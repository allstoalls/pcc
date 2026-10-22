"""Actual runtime-body relocation models; these are not native evidence."""
import ast
import os
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_set_call_slot_roots import Block, Object
from test_str_method_slot_models import StringMemory


PORT = Path(os.environ.get("PCC_SIGNATURE_PORT",
    str(Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_class.py")))


class SignatureMemory(StringMemory):
    def __init__(self, phase="allocation", fail_tuple=0, **kwargs):
        super().__init__(phase, **kwargs)
        self.fail_tuple = fail_tuple
        self.tuples_created = 0
        tree = ast.parse(PORT.read_text())
        names = {"_func_signature_valid", "_func_signature", "_bound_signature",
                 "_wrap_bound_captures", "_instance_bind_method_body"}
        body = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id.startswith("_BOUND_SIGNATURE_")
                for target in node.targets
            ):
                body.append(node)
            elif isinstance(node, ast.FunctionDef) and (
                node.name in names or node.name.startswith("_bound_signature_")
            ):
                node.decorator_list = []
                body.append(node)
        exec(compile(ast.Module(body=body, type_ignores=[]), str(PORT), "exec"), self.ns)
        self.ns.update(py_str_new=self.string, py_str_eq=self.string_equal)

    def new_tuple(self, length):
        self.tuples_created += 1
        if self.tuples_created == self.fail_tuple:
            self.error = (19, "signature tuple allocation failed")
            return None
        return super().new_tuple(length)

    def string(self, value, length):
        self.collect("allocation")
        return self.wrap(value[:length])

    def string_equal(self, lhs, rhs):
        assert lhs.alive and rhs.alive, "signature magic moved across allocation"
        return int(lhs.value == rhs.value)

    def bind(self, signature=True, spec=None):
        method = self.wrap("method")
        method.tag = abi.PY_TYPE_FUNC
        method.fields[abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] = abi.PY_TYPE_FUNC
        original = ()
        if signature:
            original = ((), spec or ("__pcc_func_signature_v1__",
                            ("self", "elem", "escapable"),
                            (0, 0, 2), (False, False, True),
                            (None, None, False)))
        method.fields[64] = self.wrap(original)
        method.fields[72] = "Parser.set_cdata_mode"
        receiver = self.wrap("receiver")
        caller = Block()
        caller.fields[0], caller.fields[8] = method, receiver
        self.roots[("caller", 0)] = caller
        self.roots[("caller", 8)] = self.add(caller, 8)
        result = self.ns["py_instance_bind_method"](
            self.read(caller, 0), self.read(caller, 8), "set_cdata_mode"
        )
        assert not self.frame_handles
        assert all(isinstance(key, tuple) for key in self.roots)
        assert all(obj.leases == 0 for obj in self.objects if obj.alive and obj is not self.none)
        return result, caller

    def unwrap(self, value):
        if not isinstance(value, Object):
            return value
        assert value.alive
        if value.tag == abi.PY_TYPE_TUPLE:
            return tuple(self.unwrap(self.read(value, 24 + i * 8)) for i in range(self.read(value, 16)))
        return value.value


@pytest.mark.parametrize("phase", ("never", "allocation", "register", "copy", "acquire",
                                  "release", "drop", "frame_enter", "frame_leave"))
def test_compiled_method_keeps_keyword_signature_across_relocation(phase):
    memory = SignatureMemory(phase)
    bound, caller = memory.bind()
    assert memory.error is None and bound.alive
    outer = bound.fields[64]
    captures, signature = outer.fields[24], outer.fields[32]
    assert captures.fields[24] is memory.read(caller, 0)
    assert captures.fields[32] is memory.read(caller, 8)
    assert memory.unwrap(signature) == (
        "__pcc_func_signature_v1__", ("elem", "escapable"),
        (0, 2), (False, True), (None, False),
    )


@pytest.mark.parametrize("phase", ("allocation", "register", "copy", "acquire",
                                  "release", "drop", "frame_enter", "frame_leave"))
def test_signatureless_builtin_keeps_original_capture_contract(phase):
    memory = SignatureMemory(phase)
    bound, caller = memory.bind(signature=False)
    assert memory.error is None and bound.alive
    assert bound.fields[64].fields[24] is memory.read(caller, 0)
    assert bound.fields[64].fields[32] is memory.read(caller, 8)


@pytest.mark.parametrize("failure", range(1, 8))
def test_allocation_failure_keeps_error_and_retires_owners(failure):
    memory = SignatureMemory("allocation", fail_tuple=failure)
    bound, caller = memory.bind()
    assert bound is None
    assert memory.error == (19, "signature tuple allocation failed")


@pytest.mark.parametrize("failure", range(1, 29))
def test_root_registration_failure_retires_partial_frames(failure):
    memory = SignatureMemory("register", fail_register=failure)
    bound, caller = memory.bind()
    assert bound is None and memory.error is not None


@pytest.mark.parametrize("failure", range(1, 25))
def test_copy_failure_retires_all_acquired_owners(failure):
    memory = SignatureMemory("copy", fail_copy=failure)
    bound, caller = memory.bind()
    assert bound is None and memory.error is not None


@pytest.mark.parametrize("phase", ("allocation", "release", "copy"))
def test_bound_signature_preserves_kinds_factory_flags_and_default_identity(phase):
    memory = SignatureMemory(phase)
    default = memory.wrap("captured default")
    spec = ("__pcc_func_signature_v1__", ("self", "arg", "items", "option", "extras"),
            (1, 0, 3, 2, 4), (False, True, False, 2, False),
            (None, default, None, default, None))
    bound, caller = memory.bind(spec=spec)
    assert memory.error is None and bound.alive
    out_signature = bound.fields[64].fields[32]
    assert memory.unwrap(out_signature)[1:4] == (
        ("arg", "items", "option", "extras"), (0, 3, 2, 4), (True, False, 2, False),
    )
    input_signature = memory.read(caller, 0).fields[64].fields[32]
    input_defaults = input_signature.fields[56]
    output_defaults = out_signature.fields[56]
    assert output_defaults.fields[24] is input_defaults.fields[32]
    assert output_defaults.fields[40] is input_defaults.fields[48]
    assert output_defaults.fields[24] is output_defaults.fields[40]
