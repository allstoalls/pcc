"""Production-body checks for the always-reserved native hidden owner."""
import ast
from pathlib import Path

import pytest

from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_set_call_slot_roots import Block, Memory


ROOT = Path(__file__).resolve().parents[2] / "pcc/runtime/py"


def _functions(filename, names, namespace):
    path = ROOT / filename
    nodes = [node for node in ast.parse(path.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


def _model(tag, flags):
    memory = Memory("none")
    cls = memory.make(None, abi.PY_TYPE_CLASS)
    cls.fields[abi.PYCLASSOBJECT_N_FIELDS_OFFSET] = 1
    # One declared field and the reserved hidden owner, as py_class_new sizes it.
    cls.fields[abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET] = (
        abi.PYINSTANCEOBJECT_FIELDS_OFFSET + 2 * abi.C_POINTER_SIZE)
    cls.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] = flags
    obj = memory.make(None, tag)
    obj.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = cls
    obj.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = memory.make("field", abi.PY_TYPE_STR)
    obj.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET + 8] = memory.make("hidden", abi.PY_TYPE_DICT)
    ns = dict(memory.ns, i64=int, abi_constant=ABI_CONSTANTS.__getitem__,
              pcc_capi_is_cext_type_tag=lambda _: 0)
    return memory, obj, ns


@pytest.mark.parametrize("flags", (0, 2, 4, 6))
@pytest.mark.parametrize("tag", (abi.PY_TYPE_INSTANCE, abi.PY_TYPE_VALUEBOX, abi.PY_TYPE_USER_CLASS_START))
def test_reserved_owner_is_in_full_and_bounded_slot_visitors(tag, flags):
    memory, obj, ns = _model(tag, flags)
    calls = []
    ns["_visit_slot"] = lambda owner, offset, role, visitor, context: calls.append((offset, role))
    _functions("freestanding_gc_object_slots.py",
               {"_visit_instance_slots", "_has_no_pointer_slots", "pcc_gc_visit_object_slots_slice"}, ns)
    assert ns["_visit_instance_slots"](obj, object(), None) == 1
    expected = [(abi.PYINSTANCEOBJECT_CLS_OFFSET, 2),
                (abi.PYINSTANCEOBJECT_FIELDS_OFFSET, 1),
                (abi.PYINSTANCEOBJECT_FIELDS_OFFSET + 8, 1)]
    assert calls == expected
    calls.clear()
    state = Block()
    assert ns["pcc_gc_visit_object_slots_slice"](obj, 0, 2, object(), None, state) == 1
    assert memory.read(state, 0) == 2
    assert ns["pcc_gc_visit_object_slots_slice"](obj, 2, 2, object(), None, state) == 1
    assert memory.read(state, 0) == -1
    assert calls == expected


@pytest.mark.parametrize("flags", (0, 2, 4, 6))
@pytest.mark.parametrize("tag", (abi.PY_TYPE_INSTANCE, abi.PY_TYPE_VALUEBOX, abi.PY_TYPE_USER_CLASS_START))
def test_relocation_extent_requires_the_reserved_owner(tag, flags):
    memory, obj, ns = _model(tag, flags)
    ns.update({"_relocate_slot_pairs_clear_destination": lambda *_: None,
               "_relocate_raw_publish_locked": lambda *_: 1,
               "_relocate_copy_payload_fail": lambda *_: -1,
               "_relocate_copy_payload_finish": lambda *_: 1})
    _functions("freestanding_gc_relocation_payload.py",
               {"pcc_gc_relocate_copy_payload_prepared_locked"}, ns)
    invoke = ns["pcc_gc_relocate_copy_payload_prepared_locked"]
    complete = abi.PYINSTANCEOBJECT_FIELDS_OFFSET + 2 * abi.C_POINTER_SIZE
    assert invoke(obj, Block(), tag, complete, Block()) == 1
    assert invoke(obj, Block(), tag, complete - abi.C_POINTER_SIZE, Block()) == -1
