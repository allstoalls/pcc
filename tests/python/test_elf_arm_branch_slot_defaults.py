"""Tiny host link/image controls; no compiler or executable invocation.

Allocation counts describe the two source-owned empty-dict sites, not native
performance, GC activity, or a bootstrap qualification.
"""

import __future__
import ast
from pathlib import Path
import struct

import pytest

from pcc.backend import elf_aarch64_relocations as arm
from pcc.backend import elf_x86_64 as elf
from pcc.backend.owned_elf_inputs import ElfInputStore
from tests.elf_arm_branch_slot_reference import (
    original_linker,
    without_arm_branch_slot_defaults,
)
from tests.python.test_elf_packed_link_classification import (
    BASE,
    FAR,
    TARGET,
    _outcome,
    _packed_object,
)


def _fixture(repeats):
    sections = []
    for name, addends in ((".text", (0, 8) * repeats),
                          (".second", (8, 8, -12) * repeats)):
        words = (0x94000000,) * len(addends) + (0xD65F03C0,)
        sections.append(elf.ElfSection(
            name, 1, 6, 8,
            b"".join(struct.pack("<I", word) for word in words),
            relocations=tuple(elf.ElfRelocation(index * 4, 2, 283, addend)
                              for index, addend in enumerate(addends)),
        ))
    sections.append(elf.ElfSection(
        ".data", 1, 3, 8, bytes(8),
        relocations=(elf.ElfRelocation(0, 2, 257, 5),),
    ))
    return elf.ElfObject(tuple(sections), (
        elf.ElfSymbol.null(),
        elf.ElfSymbol("_start", 1, 0, sections[0].size, 1, elf.STT_FUNC),
        elf.ElfSymbol("target", elf.SHN_ABS, FAR, 0, 1, elf.STT_FUNC),
    ), elf.EM_AARCH64)


def _input(tmp_path, shape, repeats):
    obj = _fixture(repeats)
    encoded = elf.emit_relocatable(obj)
    if shape == "tuple":
        return obj, {}, encoded
    if shape == "packed":
        return elf.parse_relocatable(encoded, compact_relocations=True), {}, encoded
    path = tmp_path / "branch-slots.o"
    path.write_bytes(encoded)
    store = ElfInputStore(TARGET)
    return store.stage(str(path)), {"_input_store": store}, encoded


def _counted_linker():
    """Count only the real owner's two branch-slot empty-dictionary sites."""
    source = Path(elf.__file__).read_text()
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_link_static_executable_image")
    allocations = []

    def fresh_slots():
        allocations.append(1)
        return {}

    class CountDefaults(ast.NodeTransformer):
        sites = 0

        def visit_Assign(self, node):
            if (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
                    or node.targets[0].id != "slots"):
                return self.generic_visit(node)
            value = node.value
            if isinstance(value, ast.Dict) and not value.keys:
                self.sites += 1
                node.value = ast.Call(ast.Name("fresh_slots", ast.Load()), [], [])
            elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                  and isinstance(value.func.value, ast.Name)
                  and value.func.value.id == "branch_slots"
                  and value.func.attr == "setdefault"):
                assert len(value.args) == 2
                assert isinstance(value.args[1], ast.Dict) and not value.args[1].keys
                self.sites += 1
                value.args[1] = ast.Call(ast.Name("fresh_slots", ast.Load()), [], [])
            return self.generic_visit(node)

    counter = CountDefaults()
    function = counter.visit(function)
    assert counter.sites == 2
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = dict(vars(elf), fresh_slots=fresh_slots)
    exec(compile(module, "<counted real ARM branch-slot allocations>", "exec",
                 flags=__future__.annotations.compiler_flag, dont_inherit=True),
         namespace, namespace)
    return namespace[function.name], allocations


@pytest.mark.parametrize("shape", ["tuple", "packed", "staged"])
@pytest.mark.parametrize("repeats", [0, 1, 3])
def test_one_branch_slot_dictionary_per_call_bearing_section(tmp_path, shape, repeats):
    obj, kwargs, _encoded = _input(tmp_path, shape, repeats)
    link, allocations = _counted_linker()
    result = _outcome(link, obj, **kwargs)
    assert result[0] == "image"
    assert len(allocations) == (2 if repeats else 0)


@pytest.mark.parametrize("shape", ["tuple", "packed", "staged"])
def test_repeated_calls_preserve_full_image_pool_plan_and_input_bytes(tmp_path, monkeypatch, shape):
    def run(link):
        obj, kwargs, encoded = _input(tmp_path, shape, 3)
        pools = []
        planner = arm.veneer_pool_end

        def record_pool(start, count):
            pools.append((start, count))
            return planner(start, count)

        with monkeypatch.context() as patch:
            patch.setattr(arm, "veneer_pool_end", record_pool)
            outcome = _outcome(link, obj, **kwargs)
        if shape == "staged":
            assert (tmp_path / "branch-slots.o").read_bytes() == encoded
            kwargs["_input_store"].verify()
        else:
            assert elf.emit_relocatable(obj) == encoded
        return outcome, pools

    before = run(original_linker(elf))
    after = run(elf._link_static_executable_image)
    assert after == before and after[0][0] == "image"
    # Two unique (object, symbol, addend) keys for each section, with both
    # boundaries reserved. Initialization asks the same four plans again.
    assert len(after[1]) == 8 and all(count == 2 for _start, count in after[1])


@pytest.mark.parametrize("rows", [
    ((0, 2, 283, 2), (4, 99, 283, 0)),
    ((0, 99, 311, 0), (4, 2, 283, 2)),
    ((0, 2, 283, -(1 << 63)),),
])
def test_first_relocation_error_and_explicit_cause_match(rows):
    before = _outcome(original_linker(elf), _packed_object(rows))
    after = _outcome(elf._link_static_executable_image, _packed_object(rows))
    assert after == before and after[0] != "image"


def test_immediate_preimage_rejects_unrelated_owner_drift():
    source = Path(elf.__file__).read_text()
    assert without_arm_branch_slot_defaults(source) != source
    changed = source.replace("target + addend, place", "target - addend, place", 1)
    assert changed != source
    with pytest.raises(AssertionError):
        without_arm_branch_slot_defaults(changed)
