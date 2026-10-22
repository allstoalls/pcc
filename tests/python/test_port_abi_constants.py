"""The port's ABI constants must stay derived from the C headers.

The pcc-Python ports read object fields through byte offsets. Hand-written
literals mean a C-side layout change reaches the port only if a human notices
— the drift class that produced the py_gc_track double-registration incident
shape. `pcc/runtime/py/py_abi_constants.py` is generated from the headers
by `scripts/gen_port_abi_constants.py`; this test fails if it goes stale, and
cross-checks it against the independently hand-maintained layout contract
(ARCH-P2-PORT-ABI-AUTOGEN).
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_CC_MISSING = None if shutil.which(os.environ.get("CC", "cc")) else "cc not available"


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


REPO = _repo_root()
GENERATOR = REPO / "scripts" / "gen_port_abi_constants.py"
GENERATED = REPO / "pcc" / "runtime" / "py" / "py_abi_constants.py"
GENERATED_EXPORTS = (
    REPO / "pcc" / "frontends" / "python" / "codegen" / "port_abi_exports.py"
)

PUBLIC_TYPE_TAGS: dict[str, int] = {
    "PY_TYPE_NONE": 0,
    "PY_TYPE_BOOL": 1,
    "PY_TYPE_INT": 2,
    "PY_TYPE_FLOAT": 3,
    "PY_TYPE_STR": 4,
    "PY_TYPE_LIST": 5,
    "PY_TYPE_DICT": 6,
    "PY_TYPE_TUPLE": 7,
    "PY_TYPE_SET": 8,
    "PY_TYPE_FUNC": 9,
    "PY_TYPE_CLASS": 10,
    "PY_TYPE_INSTANCE": 11,
    "PY_TYPE_EXC": 12,
    "PY_TYPE_FILE": 13,
    "PY_TYPE_ITER": 14,
    "PY_TYPE_GEN": 15,
    "PY_TYPE_COMPLEX": 16,
    "PY_TYPE_BYTES": 17,
    "PY_TYPE_BYTEARRAY": 18,
    "PY_TYPE_MEMORYVIEW": 19,
    "PY_TYPE_COROUTINE": 20,
    "PY_TYPE_WEAKREF": 21,
    "PY_TYPE_THREAD_LOCK": 22,
    "PY_TYPE_THREAD_RLOCK": 23,
    "PY_TYPE_THREAD_EVENT": 24,
    "PY_TYPE_THREAD_CONDITION": 25,
    "PY_TYPE_THREAD_SEMAPHORE": 26,
    "PY_TYPE_THREAD": 27,
    "PY_TYPE_TASK": 28,
    "PY_TYPE_CONTINUATION": 29,
    "PY_TYPE_VIRTUAL_THREAD": 30,
    "PY_TYPE_VTHREAD_CHANNEL": 31,
    "PY_TYPE_CPY_HANDLE": 32,
    "PY_TYPE_ELLIPSIS": 33,
    "PY_TYPE_NOTIMPLEMENTED": 34,
    "PY_TYPE_USER": 100,
    "PY_TYPE_PROPERTY": 101,
    "PY_TYPE_CLASSMETHOD": 102,
    "PY_TYPE_STATICMETHOD": 103,
    "PY_TYPE_USER_CLASS_START": 104,
    "PY_TYPE_VALUEBOX": 200,
    "PY_TYPE_CEXT_TAG_BASE": 65536,
}

PUBLIC_TYPE_TAG_VALUES = set(PUBLIC_TYPE_TAGS.values())

# These functions use a local ``tag`` name for an independent numeric ABI.
# Keeping the exception finite makes the public-object ratchet fail closed:
# a new private numeric domain needs explicit review rather than silently
# weakening the scanner.
PRIVATE_TAG_DOMAINS: set[tuple[str, str]] = {
    ("py_exc_table.py", "py_exc_builtin_class"),
}

# This is deliberately a finite ownership boundary, not a repository-wide
# numeric-literal grep: private collector and extension structs have their own
# contracts.  These are the public object-layout owners and consumers migrated
# by ARCH-P2-PORT-ABI-AUTOGEN.
CORE_PORT_ABI_USES: dict[str, tuple[str, ...]] = {
    "py_class.py": (
        "C_POINTER_SIZE",
        "PYOBJECTHEADER_REFCOUNT_OFFSET",
        "PYCLASSMETHOD_FUNC_OFFSET",
        "PYCLASSMETHOD_SIZE",
        "PYCLASSMETHODOBJECT_FUNC_OFFSET",
        "PYCLASSMETHODOBJECT_SIZE",
        "PYCLASSOBJECT_BASES_OFFSET",
        "PYCLASSOBJECT_FIELD_NAMES_OFFSET",
        "PYCLASSOBJECT_INSTANCE_SIZE_OFFSET",
        "PYCLASSOBJECT_METHODS_OFFSET",
        "PYCLASSOBJECT_MRO_OFFSET",
        "PYCLASSOBJECT_N_BASES_OFFSET",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYCLASSOBJECT_N_METHODS_OFFSET",
        "PYCLASSOBJECT_N_MRO_OFFSET",
        "PYCLASSOBJECT_SIZE",
        "PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET",
        "PYCLASSOBJECT_ATTRS_OFFSET",
        "PYCLASSOBJECT_METACLASS_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYINSTANCEOBJECT_FIELDS_OFFSET",
        "PYINSTANCEOBJECT_SIZE",
        "PYPROPERTYOBJECT_FGET_OFFSET",
        "PYPROPERTYOBJECT_FSET_OFFSET",
        "PYPROPERTYOBJECT_FDEL_OFFSET",
        "PYPROPERTYOBJECT_SIZE",
        "PYSTATICMETHODOBJECT_FUNC_OFFSET",
    ),
    "py_dict.py": (
        "PYDICTOBJECT_ENTRIES_OFFSET",
        "DICTENTRY_VALUE_OFFSET",
    ),
    "py_int.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_int_addsub.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_int_bigint_convert.py": ("PYINTOBJECT_NDIGITS_OFFSET",),
    "py_int_bigint_pow.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_int_bitwise.py": ("PYINTOBJECT_SIGN_OFFSET",),
    "py_int_convert.py": (
        "PYINTOBJECT_DIGITS_OFFSET",
        "PYBYTESOBJECT_DATA_OFFSET",
    ),
    "py_int_core.py": (
        "PYOBJECTHEADER_REFCOUNT_OFFSET",
        "PYINTOBJECT_NDIGITS_OFFSET",
    ),
    "py_int_decimal.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_int_mul.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_int_ops.py": (
        "PYINTOBJECT_SIGN_OFFSET",
        "PYFLOATOBJECT_VALUE_OFFSET",
    ),
    "py_int_shift.py": ("PYINTOBJECT_DIGITS_OFFSET",),
    "py_list.py": (
        "PYLISTOBJECT_LENGTH_OFFSET",
        "PYLISTOBJECT_ITEMS_OFFSET",
    ),
    "py_obj.py": (
        "PYOBJECTHEADER_FLAGS_OFFSET",
        "PYOBJECTHEADER_REFCOUNT_OFFSET",
        "PYOBJECTHEADER_TYPE_TAG_OFFSET",
    ),
    "py_obj_dealloc.py": (
        "PYLISTOBJECT_ITEMS_OFFSET",
        "PYDICTOBJECT_ENTRIES_OFFSET",
    ),
    "py_obj_ops_compare.py": (
        "C_POINTER_SIZE",
        "PYCLASSOBJECT_FIELD_NAMES_OFFSET",
        "PYCLASSOBJECT_NAME_OFFSET",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYINSTANCEOBJECT_FIELDS_OFFSET",
        "PYOBJECTHEADER_TYPE_TAG_OFFSET",
        "PYSTROBJECT_DATA_OFFSET",
        "PYTUPLEOBJECT_ITEMS_OFFSET",
    ),
    "py_obj_ops_dispatch.py": (
        "C_POINTER_SIZE",
        "PYCLASSOBJECT_MRO_OFFSET",
        "PYCLASSOBJECT_NAME_OFFSET",
        "PYCLASSOBJECT_N_MRO_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
    ),
    "py_dunder.py": (
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYOBJECTHEADER_FLAGS_OFFSET",
    ),
    "py_capi_capsule_runtime.py": (
        "C_POINTER_SIZE",
        "PYINSTANCEOBJECT_CLS_OFFSET",
    ),
    "py_exc_match.py": (
        "PYCLASSOBJECT_MRO_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
    ),
    "py_pickle_copy_runtime.py": (
        "C_POINTER_SIZE",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYINSTANCEOBJECT_FIELDS_OFFSET",
    ),
    "py_protocol_runtime.py": (
        "C_POINTER_SIZE",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYINSTANCEOBJECT_CLS_OFFSET",
        "PYINSTANCEOBJECT_FIELDS_OFFSET",
        "PYOBJECTHEADER_FLAGS_OFFSET",
    ),
    "py_exc_traceback.py": ("PYCLASSOBJECT_NAME_OFFSET",),
    "py_format_runtime.py": ("PYCLASSOBJECT_NAME_OFFSET",),
    "py_str.py": ("PYSTROBJECT_DATA_OFFSET", "PYSTROBJECT_SIZE"),
    "py_str_accessors.py": (
        "PYSTROBJECT_DATA_OFFSET",
        "PYBYTESOBJECT_DATA_OFFSET",
    ),
    "py_str_slice.py": ("PYSTROBJECT_DATA_OFFSET", "PYSTROBJECT_SIZE"),
    "py_substrate.py": (
        "C_POINTER_SIZE",
        "PYCLASSOBJECT_BASES_OFFSET",
        "PYCLASSOBJECT_FIELD_NAMES_OFFSET",
        "PYCLASSOBJECT_INSTANCE_SIZE_OFFSET",
        "PYCLASSOBJECT_METHODS_OFFSET",
        "PYCLASSOBJECT_MRO_OFFSET",
        "PYCLASSOBJECT_N_BASES_OFFSET",
        "PYCLASSOBJECT_N_FIELDS_OFFSET",
        "PYCLASSOBJECT_N_METHODS_OFFSET",
        "PYCLASSOBJECT_N_MRO_OFFSET",
        "PYCLASSOBJECT_SIZE",
        "PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET",
        "PYINSTANCEOBJECT_SIZE",
    ),
    "py_tuple.py": (
        "PYTUPLEOBJECT_LEN_OFFSET",
        "PYTUPLEOBJECT_ITEMS_OFFSET",
    ),
}


def test_generated_file_exists_and_is_marked_generated():
    text = GENERATED.read_text(encoding="utf-8")
    assert "GENERATED by scripts/gen_port_abi_constants.py" in text
    assert "Do not edit by hand" in text


def test_runtime_library_codegen_inlines_generated_abi_constant_imports(tmp_path):
    """Single-object runtime builds must not turn ABI constants into CPython.

    The production archive compiles each pcc-Python runtime owner separately.
    A generated constant import therefore needs the same static export metadata
    in the single-file pipeline that closed-world application builds derive
    from sibling modules.  Otherwise every function using a layout constant is
    silently replaced by a strict no-libpython stub.
    """
    from pcc.frontends.python import pipeline

    source = REPO / "pcc" / "runtime" / "py" / "py_dict.py"
    llvm_ir = tmp_path / "py_dict.ll"
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert "strict.nolib.stub:" not in ir_text
    cpy_calls = [
        line
        for line in ir_text.splitlines()
        if " call " in line and "@py_cpy_" in line
    ]
    assert cpy_calls == []
    # LLVM's omitted linkage and explicit external linkage name the same
    # exported zero-argument function ABI. Keep internal/private definitions
    # and a changed return type or parameter list outside this contract.
    definitions = [line for line in ir_text.splitlines()
                   if line.startswith("define ") and "@py_dict_new(" in line]
    assert len(definitions) == 1, definitions
    assert re.fullmatch(
        r"define (?:external )?ptr @py_dict_new\(\)(?: [^{\n]*)? \{",
        definitions[0],
    ), definitions[0]
    assert "name.dynamic.PYDICTOBJECT_ITEM_COUNT_OFFSET" not in ir_text


def test_imported_abi_tag_can_initialize_unsafe_native_global(tmp_path):
    """A generated constant stays usable where LLVM needs a constant expr."""
    from pcc.frontends.python import pipeline

    source = REPO / "pcc" / "runtime" / "py" / "py_substrate.py"
    llvm_ir = tmp_path / "imported_abi_global.ll"
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    assert "@py_next_user_tag = global i32 104" in ir_text


def test_generated_type_tag_preserves_allocator_i32_abi(tmp_path):
    """A boxed static tag must be narrowed to pcc_gc_alloc's real ABI."""
    from pcc.frontends.python import pipeline
    from pcc.tools.ir_to_obj import emit_object

    source = (
        REPO
        / "pcc" / "runtime"
        / "py"
        / "py_capi_buffer_runtime.py"
    )
    llvm_ir = tmp_path / "py_capi_buffer_runtime.ll"
    pipeline.compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")
    declarations = [line for line in ir_text.splitlines()
                    if line.startswith("declare ") and "@pcc_gc_alloc(" in line]
    assert len(declarations) == 1, declarations
    assert re.fullmatch(
        r"declare (?:external )?ptr @pcc_gc_alloc\(i64, i32, i32\)",
        declarations[0],
    ), declarations[0]
    calls = [line for line in ir_text.splitlines()
             if " call " in line and "@pcc_gc_alloc(" in line]
    assert calls
    for call in calls:
        assert re.search(
            r"\bcall ptr(?: \(i64, i32, i32\))? @pcc_gc_alloc\("
            r"i64 [^,\n]+, i32 [^,\n]+, i32 [^)\n]+\)",
            call,
        ), call
    assert emit_object(ir_text)


@pytest.mark.pcc_gate(unavailable=_CC_MISSING)
def test_generated_constants_are_not_stale_against_the_c_headers():
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    proc = subprocess.run(
        ["uv", "run", "python", str(GENERATOR), "--check"],
        capture_output=True, text=True, timeout=280, cwd=str(REPO), env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_generated_values_agree_with_the_hand_written_layout_contract():
    """Two independent sources must say the same thing.

    `test_runtime_layout_contract.py` pins offsets by hand; this module
    derives them from the headers. If they disagree, one of them is wrong and
    the ports are reading the wrong bytes either way.
    """
    from tests.python.test_runtime_layout_contract import (
        EXPECTED_OFFSETS,
        EXPECTED_SIZES,
    )
    import pcc.runtime.py.py_abi_constants as abi

    required_generated_structs = {
        "PyClassObject",
        "PyClassMethod",
        "PyInstanceObject",
        "PyPropertyObject",
        "PyClassMethodObject",
        "PyStaticMethodObject",
    }
    mismatches = []
    for (struct, field), expected in EXPECTED_OFFSETS.items():
        name = f"{struct}_{field}".upper() + "_OFFSET"
        got = getattr(abi, name, None)
        if got is None:
            if struct in required_generated_structs:
                mismatches.append(f"{name}: missing from generated ABI")
            continue
        if got != expected:
            mismatches.append(f"{name}: generated {got}, contract {expected}")
    for struct, expected in EXPECTED_SIZES.items():
        name = struct.upper() + "_SIZE"
        got = getattr(abi, name, None)
        if got is None and struct in required_generated_structs:
            mismatches.append(f"{name}: missing from generated ABI")
            continue
        if got is not None and got != expected:
            mismatches.append(f"{name}: generated {got}, contract {expected}")
    assert not mismatches, "generated ABI disagrees with the layout contract:\n  " + "\n  ".join(mismatches)


def test_type_tags_and_flags_are_present_and_distinct():
    import pcc.runtime.py.py_abi_constants as abi

    tags = {
        name: value
        for name, value in vars(abi).items()
        if name.startswith("PY_TYPE_") and isinstance(value, int)
    }
    assert tags == PUBLIC_TYPE_TAGS
    assert tags["PY_TYPE_USER_CLASS_START"] == tags["PY_TYPE_STATICMETHOD"] + 1
    assert tags["PY_TYPE_PROPERTY"] == tags["PY_TYPE_USER"] + 1

    flags = {
        name: value
        for name, value in vars(abi).items()
        if name.startswith("PY_FLAG_") and isinstance(value, int)
    }
    assert len(flags) >= 3, sorted(flags)
    assert all(v and (v & (v - 1)) == 0 for v in flags.values()), flags


def test_type_tag_inventory_is_discovered_from_every_runtime_header():
    """The generator must not carry a second hand-curated tag allowlist."""
    source = GENERATOR.read_text(encoding="utf-8")
    assert "TYPE_TAGS:" not in source
    assert "_type_tags_from_headers()" in source

    declared: set[str] = set()
    patterns = (
        re.compile(r"^\s*(PY_TYPE_[A-Z0-9_]+)\s*=", re.MULTILINE),
        re.compile(r"^\s*#define\s+(PY_TYPE_[A-Z0-9_]+)\b", re.MULTILINE),
    )
    for path in (
        REPO / "pcc" / "runtime" / "include" / "py_runtime.h",
        REPO / "pcc" / "runtime" / "src" / "py_internal.h",
    ):
        header = path.read_text(encoding="utf-8")
        for pattern in patterns:
            declared.update(pattern.findall(header))
    assert declared == set(PUBLIC_TYPE_TAGS)


def test_ambiguous_size_field_uses_semantic_generated_name():
    import pcc.runtime.py.py_abi_constants as abi

    assert abi.PYDICTOBJECT_ITEM_COUNT_OFFSET == 16
    assert abi.PYDICTOBJECT_SIZE == 56
    assert not hasattr(abi, "PYDICTOBJECT_SIZE_OFFSET")


def _is_header_type_tag_load(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return False
    if node.func.id in {"_type_of", "py_type_of", "_type_tag"}:
        return True
    if node.func.id != "load_i32" or len(node.args) < 2:
        return False
    offset = node.args[1]
    if isinstance(offset, ast.Name):
        return offset.id == "PYOBJECTHEADER_TYPE_TAG_OFFSET"
    if isinstance(offset, ast.Constant):
        return offset.value == 8
    return (
        isinstance(offset, ast.Call)
        and isinstance(offset.func, ast.Name)
        and offset.func.id == "abi_constant"
        and len(offset.args) == 1
        and isinstance(offset.args[0], ast.Constant)
        and offset.args[0].value == "object.header.type_tag_offset"
    )


class _RawPublicTagVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str):
        self.path = path
        self.source = source
        self.function = ""
        self.violations: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prior = self.function
        self.function = node.name
        self.generic_visit(node)
        self.function = prior

    def _record(self, node: ast.AST, value: int) -> None:
        self.violations.append(
            f"{self.path.name}:{node.lineno}: raw public type tag {value}"
        )

    def _private_tag_domain(self) -> bool:
        return (
            (self.path.name, self.function) in PRIVATE_TAG_DOMAINS
            or self.function.startswith("py_subs_exc_")
        )

    @staticmethod
    def _tag_name(node: ast.AST) -> bool:
        # ``exception_*`` names hold builtin exception tags (OSError is 14),
        # a separate numeric domain like PRIVATE_TAG_DOMAINS.
        return isinstance(node, ast.Name) and not node.id.startswith("exception_") and (
            node.id == "tag"
            or node.id == "type_tag"
            or node.id.endswith("_tag")
            or node.id.startswith("tag_")
        )

    @classmethod
    def _tag_expression(cls, node: ast.AST) -> bool:
        return cls._tag_name(node) or _is_header_type_tag_load(node)

    def _check_pair(self, tag_side: ast.AST, value_side: ast.AST) -> None:
        if not self._tag_expression(tag_side):
            return
        if (
            isinstance(value_side, ast.Constant)
            and type(value_side.value) is int
            and value_side.value in PUBLIC_TYPE_TAG_VALUES
        ):
            self._record(value_side, value_side.value)

    def visit_Compare(self, node: ast.Compare) -> None:
        if not self._private_tag_domain():
            operands = [node.left, *node.comparators]
            for left, right in zip(operands, operands[1:]):
                self._check_pair(left, right)
                self._check_pair(right, left)
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, (ast.Add, ast.Sub)) and not self._private_tag_domain():
            for tag_side, value_side in ((node.left, node.right), (node.right, node.left)):
                if (
                    self._tag_expression(tag_side)
                    and isinstance(value_side, ast.Constant)
                    and type(value_side.value) is int
                    and value_side.value == PUBLIC_TYPE_TAGS["PY_TYPE_CEXT_TAG_BASE"]
                ):
                    self._record(value_side, value_side.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if (
                node.func.id == "pcc_gc_alloc"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in PUBLIC_TYPE_TAG_VALUES
            ):
                self._record(node.args[1], node.args[1].value)
            if (
                node.func.id == "store_i32"
                and len(node.args) >= 3
                and _is_header_type_tag_load(
                    ast.Call(
                        func=ast.Name(id="load_i32"),
                        args=node.args[:2],
                        keywords=[],
                    )
                )
                and isinstance(node.args[2], ast.Constant)
                and node.args[2].value in PUBLIC_TYPE_TAG_VALUES
                and (self.path.name, self.function)
                not in {
                    ("freestanding_re_engine.py", "_compile"),
                }
            ):
                self._record(node.args[2], node.args[2].value)
            if (
                node.func.id == "define_global_i32"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "py_next_user_tag"
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in PUBLIC_TYPE_TAG_VALUES
            ):
                self._record(node.args[1], node.args[1].value)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if (
            self.function in {"_type_of", "_type_tag", "py_builtin_type_class_tag"}
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
            and node.value.value in PUBLIC_TYPE_TAG_VALUES
        ):
            self._record(node.value, node.value.value)
        self.generic_visit(node)


@pytest.mark.parametrize(
    ("filename", "function", "expression", "expected"),
    [
        ("freestanding_time_format.py", "strftime", "load_i32(tm, TM_HOUR_OFFSET) < 12", []),
        ("freestanding_time_format.py", "strftime", "load_i32(obj, 8) < 12", [12]),
        ("freestanding_time_format.py", "other", "load_i32(tm, 8) < 12", [12]),
        ("other.py", "strftime", "load_i32(tm, 8) < 12", [12]),
        ("freestanding_time_format.py", "strftime",
         "load_i32(tm, PYOBJECTHEADER_TYPE_TAG_OFFSET) == 12", [12]),
        ("freestanding_time_format.py", "strftime", "tag == 12", [12]),
        ("py_obj.py", "decref", "tag_dbg >= 0x10000", [65536]),
        ("py_capi_visit_runtime.py", "visit", "tag - 0x10000", [65536]),
        ("py_obj.py", "alloc", "flags & 65536", []),
        ("py_capi_cext_runtime.py", "decode_i16", "value - 65536", []),
        ("py_obj.py", "alloc", "tag + 1", []),
        ("py_exc_objects.py", "ctor", "exception_tag == 14", []),
        ("py_exc_objects.py", "ctor", "type_tag == 14", [14]),
    ],
)
def test_raw_tag_scanner_preserves_numeric_domains(filename, function, expression, expected):
    source = f"def {function}(tm, obj, tag, tag_dbg, flags, value):\n    return {expression}\n"
    visitor = _RawPublicTagVisitor(Path(filename), source)
    visitor.visit(ast.parse(source))
    assert visitor.violations == [
        f"{filename}:2: raw public type tag {value}" for value in expected
    ]


def test_public_type_tags_are_never_reintroduced_as_raw_literals():
    violations: list[str] = []
    for path in sorted((REPO / "pcc" / "runtime" / "py").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        visitor = _RawPublicTagVisitor(path, source)
        visitor.visit(ast.parse(source, filename=str(path)))
        violations.extend(visitor.violations)
    assert not violations, "raw public type tags returned:\n  " + "\n  ".join(violations)


def test_generated_type_tags_are_not_used_as_offsets_or_private_enum_values():
    """A numeric-to-name rewrite must preserve the numeric domain.

    Public tags overlap offsets, slot roles, regex limits, and CPython's
    structmember codes.  Replacing one of those unrelated numbers with a
    PY_TYPE_* name is behaviorally wrong even when the integer happens to be
    equal today.
    """
    port_root = REPO / "pcc" / "runtime" / "py"
    violations: list[str] = []
    for path in sorted(port_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            offset_args: list[ast.AST] = []
            if node.func.id.startswith("load_") and len(node.args) >= 2:
                offset_args = [node.args[1]]
            elif node.func.id.startswith("store_") and len(node.args) >= 2:
                offset_args = [node.args[1]]
            elif node.func.id in {"ptr_add", "ptr_sub"} and len(node.args) >= 2:
                offset_args = [node.args[1]]
            for offset in offset_args:
                names = {
                    child.id
                    for child in ast.walk(offset)
                    if isinstance(child, ast.Name)
                }
                bad = sorted(name for name in names if name.startswith("PY_TYPE_"))
                if bad:
                    violations.append(
                        f"{path.name}:{node.lineno}: type tag used as offset: {bad}"
                    )

    private_domain_sources = {
        "freestanding_gc_mapped_roots.py": ("stack_offset >= PY_TYPE_",),
        "freestanding_gc_relocation_payload.py": ("role == PY_TYPE_",),
        "freestanding_re_engine.py": ("guard >= PY_TYPE_",),
        "py_capi_cext_runtime.py": ("member_type == PY_TYPE_",),
    }
    for filename, forbidden in private_domain_sources.items():
        source = (port_root / filename).read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in source:
                violations.append(f"{filename}: public tag used in private numeric domain")
    assert not violations, "type-tag domain confusion:\n  " + "\n  ".join(violations)


def test_py_class_does_not_mix_named_instance_tag_with_literal_11():
    source = (REPO / "pcc" / "runtime" / "py" / "py_class.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(_is_header_type_tag_load(child) for operand in operands for child in ast.walk(operand)) and not any(
            isinstance(child, ast.Name) and child.id == "tag"
            for operand in operands
            for child in ast.walk(operand)
        ):
            continue
        if any(
            isinstance(child, ast.Constant)
            and type(child.value) is int
            and child.value == 11
            for operand in operands
            for child in ast.walk(operand)
        ):
            violations.append(node.lineno)
    assert not violations, f"py_class.py uses raw PY_TYPE_INSTANCE at {violations}"


def test_freestanding_type_aliases_project_the_complete_generated_inventory():
    from pcc.runtime.freestanding_abi_spec import ABI_SPEC

    projected = {
        "object.type." + name[len("PY_TYPE_"):].lower(): value
        for name, value in PUBLIC_TYPE_TAGS.items()
    }
    assert {name: ABI_SPEC.get(name) for name in projected} == projected


def test_compiler_type_tag_aliases_cover_generated_inventory():
    """Frontend tag names are aliases over the generated C-header values."""
    from pcc.frontends.python.codegen import freestanding_abi_constants as compiler_abi
    import pcc.runtime.py.py_abi_constants as runtime_abi

    generated = {
        name: value
        for name, value in vars(runtime_abi).items()
        if name.startswith("PY_TYPE_") and isinstance(value, int)
    }
    assert generated
    assert {
        name: getattr(compiler_abi, name, None)
        for name in generated
    } == generated


def test_generated_compiler_type_tag_aliases_are_static_integer_constants():
    """Self-hosted module initialization must not load tag aliases from a dict."""
    from pcc.runtime.freestanding_abi_spec import ABI_SPEC

    path = (
        REPO
        / "pcc" / "frontends" / "python"
        / "codegen"
        / "freestanding_abi_constants.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignments = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id.startswith("PY_TYPE_")
        and isinstance(node.value, ast.Constant)
        and type(node.value.value) is int
    }
    expected = {
        "PY_TYPE_" + name.removeprefix("object.type.").upper(): value
        for name, value in ABI_SPEC.items()
        if name.startswith("object.type.")
    }
    assert assignments == expected


def test_frontend_type_tag_dispatch_tables_match_generated_aliases():
    from pcc.frontends.python.codegen import freestanding_abi_constants as abi
    from pcc.frontends.python.codegen.compare_membership_lowering import (
        _BUILTIN_TYPE_TAGS as compare_tags,
    )
    from pcc.frontends.python.codegen.isinstance_lowering import (
        _BUILTIN_TYPE_TAGS as isinstance_tags,
    )

    expected = {
        "bool": abi.PY_TYPE_BOOL,
        "int": abi.PY_TYPE_INT,
        "float": abi.PY_TYPE_FLOAT,
        "str": abi.PY_TYPE_STR,
        "list": abi.PY_TYPE_LIST,
        "dict": abi.PY_TYPE_DICT,
        "tuple": abi.PY_TYPE_TUPLE,
        "set": abi.PY_TYPE_SET,
        "bytes": abi.PY_TYPE_BYTES,
        "bytearray": abi.PY_TYPE_BYTEARRAY,
    }
    assert compare_tags == expected
    assert isinstance_tags == {
        "NoneType": abi.PY_TYPE_NONE,
        **expected,
        "FunctionType": abi.PY_TYPE_FUNC,
        "type": abi.PY_TYPE_CLASS,
        # isinstance(x, complex/memoryview) is answered natively (056e61852).
        "complex": abi.PY_TYPE_COMPLEX,
        "memoryview": abi.PY_TYPE_MEMORYVIEW,
    }


def test_static_float_literal_header_uses_generated_type_tag():
    from pcc.frontends.python.codegen import marshal
    from pcc.ir.compat import ir
    from pcc.runtime.py import py_abi_constants as abi

    module = ir.Module(name="float_tag_abi")
    literal = marshal._float_literal_object(module, 0.5)
    tag = literal.initializer.value[1]
    assert tag.type == ir.IntType(32)
    assert tag.value == abi.PY_TYPE_FLOAT
    assert marshal._float_literal_object(module, 0.5) is literal


def _runtime_symbol_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Subscript):
        return None
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def _is_runtime_type_tag_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    return any(
        _runtime_symbol_name(child) in {"py_obj_type_tag", "pcc_py_type_of"}
        for child in ast.walk(node.func)
    )


def _raw_ir_integer_constant(node: ast.AST) -> int | None:
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "Constant":
        return None
    value = node.args[1]
    if isinstance(value, ast.Constant) and type(value.value) is int:
        return value.value
    return None


def test_frontend_type_tag_dispatch_does_not_copy_numeric_abi():
    """Runtime tag dispatch must consume generated aliases, not copied ints."""
    codegen_root = REPO / "pcc" / "frontends" / "python" / "codegen"
    violations: list[str] = []
    for path in sorted(codegen_root.glob("*.py")):
        if path.name == "freestanding_abi_constants.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for assign in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ):
            targets = (
                assign.targets
                if isinstance(assign, ast.Assign)
                else [assign.target]
            )
            value = assign.value
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id.startswith(("PY_TYPE_", "_PY_TYPE_"))
                    and isinstance(value, ast.Constant)
                    and type(value.value) is int
                ):
                    violations.append(
                        f"{path.name}:{assign.lineno}: copied {target.id}"
                    )
                if (
                    isinstance(target, ast.Name)
                    and target.id in {"_BUILTIN_TYPE_TAGS", "builtin_tags"}
                    and isinstance(value, ast.Dict)
                ):
                    for item in value.values:
                        if (
                            isinstance(item, ast.Constant)
                            and type(item.value) is int
                            and item.value >= 0
                        ):
                            violations.append(
                                f"{path.name}:{item.lineno}: copied builtin type tag"
                            )

        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            tag_names: set[str] = set()
            for assign in (
                node
                for node in ast.walk(function)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
            ):
                targets = (
                    assign.targets
                    if isinstance(assign, ast.Assign)
                    else [assign.target]
                )
                if assign.value is not None and _is_runtime_type_tag_call(
                    assign.value
                ):
                    tag_names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
            for call in (
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
            ):
                if not (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr in {"icmp_signed", "icmp_unsigned"}
                ):
                    continue
                compares_tag = any(
                    _is_runtime_type_tag_call(child)
                    or (isinstance(child, ast.Name) and child.id in tag_names)
                    for arg in call.args
                    for child in ast.walk(arg)
                )
                if not compares_tag:
                    continue
                for child in ast.walk(call):
                    raw = _raw_ir_integer_constant(child)
                    if raw is not None:
                        violations.append(
                            f"{path.name}:{child.lineno}: raw tag comparison {raw}"
                        )

    assert not violations, "frontend copied public object tags:\n  " + "\n  ".join(
        violations
    )


def test_freestanding_layout_aliases_project_generated_class_records():
    from pcc.runtime.freestanding_abi_spec import ABI_SPEC
    import pcc.runtime.py.py_abi_constants as abi

    expected = {
        "object.pointer.size": abi.C_POINTER_SIZE,
        "object.class.n_bases_offset": abi.PYCLASSOBJECT_N_BASES_OFFSET,
        "object.class.bases_offset": abi.PYCLASSOBJECT_BASES_OFFSET,
        "object.class.n_mro_offset": abi.PYCLASSOBJECT_N_MRO_OFFSET,
        "object.class.mro_offset": abi.PYCLASSOBJECT_MRO_OFFSET,
        "object.class.n_methods_offset": abi.PYCLASSOBJECT_N_METHODS_OFFSET,
        "object.class.methods_offset": abi.PYCLASSOBJECT_METHODS_OFFSET,
        "object.class.n_fields_offset": abi.PYCLASSOBJECT_N_FIELDS_OFFSET,
        "object.class.field_names_offset": abi.PYCLASSOBJECT_FIELD_NAMES_OFFSET,
        "object.class_method.size": abi.PYCLASSMETHOD_SIZE,
        "object.class_method.func_offset": abi.PYCLASSMETHOD_FUNC_OFFSET,
        "object.instance.size": abi.PYINSTANCEOBJECT_SIZE,
        "object.instance.cls_offset": abi.PYINSTANCEOBJECT_CLS_OFFSET,
        "object.instance.fields_offset": abi.PYINSTANCEOBJECT_FIELDS_OFFSET,
        "object.property.fget_offset": abi.PYPROPERTYOBJECT_FGET_OFFSET,
        "object.classmethod.func_offset": abi.PYCLASSMETHODOBJECT_FUNC_OFFSET,
        "object.staticmethod.func_offset": abi.PYSTATICMETHODOBJECT_FUNC_OFFSET,
    }
    assert {key: ABI_SPEC.get(key) for key in expected} == expected


def test_freestanding_descriptor_slot_visitors_use_generated_layouts():
    source = (
        REPO
        / "pcc" / "runtime"
        / "py"
        / "freestanding_gc_object_slots.py"
    ).read_text(encoding="utf-8")
    for alias in (
        "object.property.fget_offset",
        "object.property.fset_offset",
        "object.property.fdel_offset",
        "object.classmethod.func_offset",
        "object.staticmethod.func_offset",
    ):
        assert f'abi_constant("{alias}")' in source


def test_runtime_comments_do_not_copy_public_numeric_type_tags():
    violations: list[str] = []
    port_root = REPO / "pcc" / "runtime" / "py"
    for path in sorted(port_root.glob("*.py")):
        if path == GENERATED:
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"\bPY_TYPE_[A-Z0-9_]+\s*(?:=|==)\s*\d+", source):
            violations.append(f"{path.name}: copied numeric type tag")
    assert not violations, "generated type tags copied into prose:\n  " + "\n  ".join(violations)


def test_core_runtime_docstrings_do_not_copy_generated_layouts():
    violations: list[str] = []
    for filename in ("py_obj.py", "py_obj_ops_dispatch.py", "py_dict.py"):
        path = REPO / "pcc" / "runtime" / "py" / filename
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        if re.search(r"\boffset\s+\d+\s+\w+", doc):
            violations.append(f"{filename}: copied numeric struct layout")
    assert not violations, "generated layouts copied into prose:\n  " + "\n  ".join(violations)


@pytest.mark.parametrize(
    ("filename", "constants"),
    sorted(CORE_PORT_ABI_USES.items()),
)
def test_core_port_readers_consume_generated_abi_constants(filename, constants):
    """A generated file alone is insufficient if owners still use literals."""
    path = REPO / "pcc" / "runtime" / "py" / filename
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = {
        alias.asname or alias.name: alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "pcc.runtime.py.py_abi_constants"
        for alias in node.names
    }
    loaded = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    for name in constants:
        assert imported.get(name) == name, f"{filename} does not import {name}"
        assert name in loaded, f"{filename} imports but does not use {name}"


def test_core_object_owners_do_not_reintroduce_raw_header_offsets():
    """The most dangerous universal header offsets have no private meaning."""
    owners = (
        "py_class.py",
        "py_int_core.py",
        "py_obj.py",
        "py_obj_dealloc.py",
    )
    forbidden = (
        "load_i32(o, 8)",
        "load_i32(o, 12)",
        "load_i64(o, 0)",
        "store_i32(o, 8,",
        "store_i32(o, 12,",
        "store_i64(o, 0,",
    )
    violations: list[str] = []
    for filename in owners:
        text = (REPO / "pcc" / "runtime" / "py" / filename).read_text(
            encoding="utf-8"
        )
        for needle in forbidden:
            if needle in text:
                violations.append(f"{filename}: {needle}")
    assert not violations, "raw PyObjectHeader offsets returned:\n  " + "\n  ".join(violations)
