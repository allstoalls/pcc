"""Single source of truth for compiler-owned freestanding runtime ABIs."""

from pcc.backend import precise_stackmap as _stackmap_abi
from pcc.runtime.py import py_abi_constants as _object_abi


# Export every C-header-derived object type through the compiler-facing
# ``abi_constant("object.type.*")`` namespace.  This is an alias projection,
# not a second numeric authority: adding a generated PY_TYPE_* automatically
# makes the corresponding freestanding key available.
_OBJECT_TYPE_ABI = {
    "object.type." + name[len("PY_TYPE_"):].lower(): value
    for name, value in vars(_object_abi).items()
    if name.startswith("PY_TYPE_") and isinstance(value, int)
}


# Project every generated public object layout into the compiler-owned
# ``abi_constant`` namespace.  These are aliases over py_abi_constants, not a
# second hand-maintained set of offsets.  Longest prefixes come first because
# PyClassMethodObject and PyClassMethod are distinct ABI records.
_OBJECT_LAYOUT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("PYVTHREADCHANNELENDPOINTOBJECT", "object.vthread_channel.endpoint"),
    ("PYVTHREADCHANNELCOREOBJECT", "object.vthread_channel.core"),
    ("PYVIRTUALTHREADOBJECT", "object.virtual_thread"),
    ("PYCLASSMETHODOBJECT", "object.classmethod"),
    ("PYSTATICMETHODOBJECT", "object.staticmethod"),
    ("PYPROPERTYOBJECT", "object.property"),
    ("PYCLASSMETHOD", "object.class_method"),
    ("PYCLASSOBJECT", "object.class"),
    ("PYINSTANCEOBJECT", "object.instance"),
    ("PYOBJECTHEADER", "object.header"),
    ("PYMEMORYVIEWOBJECT", "object.memoryview"),
    ("PYBYTEARRAYOBJECT", "object.bytearray"),
    ("PYBYTESOBJECT", "object.bytes"),
    ("PYCOMPLEXOBJECT", "object.complex"),
    ("PYDICTOBJECT", "object.dict"),
    ("PYFLOATOBJECT", "object.float"),
    ("PYINTOBJECT", "object.int"),
    ("PYLISTOBJECT", "object.list"),
    ("PYSTROBJECT", "object.str"),
    ("PYTUPLEOBJECT", "object.tuple"),
    ("DICTENTRY", "object.dict_entry"),
)


def _object_layout_aliases() -> dict[str, int]:
    aliases: dict[str, int] = {}
    for constant, value in vars(_object_abi).items():
        if not isinstance(value, int):
            continue
        for prefix, key_prefix in _OBJECT_LAYOUT_PREFIXES:
            if constant == prefix + "_SIZE":
                aliases[key_prefix + ".size"] = value
                break
            if constant.startswith(prefix + "_") and constant.endswith("_OFFSET"):
                field = constant[len(prefix) + 1 : -len("_OFFSET")].lower()
                aliases[key_prefix + "." + field + "_offset"] = value
                break
    return aliases


_OBJECT_LAYOUT_ABI = _object_layout_aliases()


ABI_SPEC: dict[str, int] = {
    **_OBJECT_LAYOUT_ABI,
    "object.pointer.size": _object_abi.C_POINTER_SIZE,
    "object.dict_entry.key_offset": _object_abi.DICTENTRY_KEY_OFFSET,
    "object.dict_entry.size": _object_abi.DICTENTRY_SIZE,
    "object.dict_entry.value_offset": _object_abi.DICTENTRY_VALUE_OFFSET,
    "object.bytes.byte_len_offset": _object_abi.PYBYTESOBJECT_BYTE_LEN_OFFSET,
    "object.bytes.data_offset": _object_abi.PYBYTESOBJECT_DATA_OFFSET,
    "object.class.attrs_offset": _object_abi.PYCLASSOBJECT_ATTRS_OFFSET,
    "object.class.del_method_offset": _object_abi.PYCLASSOBJECT_DEL_METHOD_OFFSET,
    "object.class.metaclass_offset": _object_abi.PYCLASSOBJECT_METACLASS_OFFSET,
    "object.dict.entries_offset": _object_abi.PYDICTOBJECT_ENTRIES_OFFSET,
    "object.dict.entries_used_offset": _object_abi.PYDICTOBJECT_ENTRIES_USED_OFFSET,
    "object.list.items_offset": _object_abi.PYLISTOBJECT_ITEMS_OFFSET,
    "object.list.length_offset": _object_abi.PYLISTOBJECT_LENGTH_OFFSET,
    "object.memoryview.base_offset": _object_abi.PYMEMORYVIEWOBJECT_BASE_OFFSET,
    "object.header.flags_offset": _object_abi.PYOBJECTHEADER_FLAGS_OFFSET,
    "object.header.type_tag_offset": _object_abi.PYOBJECTHEADER_TYPE_TAG_OFFSET,
    "object.tuple.items_offset": _object_abi.PYTUPLEOBJECT_ITEMS_OFFSET,
    "object.tuple.length_offset": _object_abi.PYTUPLEOBJECT_LEN_OFFSET,
    "object.flag.gc_tracked": _object_abi.PY_FLAG_GC_TRACKED,
    **_OBJECT_TYPE_ABI,
    "stackmap.magic_i64": _stackmap_abi.MAGIC_I64,
    "stackmap.header_size": _stackmap_abi.HEADER_SIZE,
    "stackmap.function_size": _stackmap_abi.FUNCTION_SIZE,
    "stackmap.record_size": _stackmap_abi.RECORD_SIZE,
    "stackmap.location_size": _stackmap_abi.LOCATION_SIZE,
    "stackmap.no_offset": _stackmap_abi.NO_OFFSET,
    "stackmap.location.stack_indirect": _stackmap_abi.LOCATION_STACK_INDIRECT,
    "stackmap.location.managed": _stackmap_abi.LOCATION_MANAGED,
    "stackmap.location.owned": _stackmap_abi.LOCATION_OWNED,
    "stackmap.runtime.header_size": _stackmap_abi.RUNTIME_HEADER_SIZE,
    "stackmap.runtime.function_size": _stackmap_abi.RUNTIME_FUNCTION_SIZE,
    "stackmap.runtime.record_size": _stackmap_abi.RUNTIME_RECORD_SIZE,
    "stackmap.runtime.live_set_size": _stackmap_abi.RUNTIME_LIVE_SET_SIZE,
    "stackmap.runtime.location_size": _stackmap_abi.RUNTIME_LOCATION_SIZE,
    "stackmap.runtime.rare_size": _stackmap_abi.RUNTIME_RARE_SIZE,
    "stdio.file.magic": 5783538579059651889,
    "stdio.file.size": 64,
    "stdio.file.magic_offset": 0,
    "stdio.file.fd_offset": 8,
    "stdio.file.flags_offset": 16,
    "stdio.file.aux_offset": 24,
    "stdio.file.buffer_offset": 32,
    "stdio.file.buffer_capacity_offset": 40,
    "stdio.file.buffer_length_offset": 48,
    "stdio.file.buffer_position_offset": 56,
    "stdio.flag.readable": 1,
    "stdio.flag.writable": 2,
    "stdio.flag.error": 4,
    "stdio.flag.eof": 8,
    "stdio.flag.standard": 16,
    "stdio.flag.append": 32,
    "stdio.flag.borrowed_descriptor": 64,
    "stdio.flag.input_standard": 17,
    "stdio.flag.output_standard": 18,
}


STDIO_FILE_FIELDS: tuple[tuple[str, str], ...] = (
    ("magic", "uint64_t"),
    ("fd", "int64_t"),
    ("flags", "uint64_t"),
    ("aux", "int64_t"),
    ("buffer", "void *"),
    ("buffer_capacity", "uint64_t"),
    ("buffer_length", "uint64_t"),
    ("buffer_position", "int64_t"),
)
