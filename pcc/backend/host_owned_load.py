"""Load owned AArch64 objects into the host CPython process.

This API boundary uses only the standard library and the platform ABI. Native
pcc1 executes its emitted files; it never imports this host adapter.
"""

from __future__ import annotations

import ctypes
import mmap
import os
import struct
import threading

from . import macho_spec as spec
from .arm64_asm_driver import assemble_file
from .macho_exec import _adrp_imm, _branch26, _pageoff12, prepare_executable_object
from .native_object import NativeObject
from .self_backend_dispatch import emit_self_asm


_CALL_LOCK = threading.RLock()
# An integer ABI address cannot carry or weakly expose an allocation owner.
# Keep these explicit opaque-pointer allocations alive for the process.
_OPAQUE_POINTER_OWNERS = []


def load_functions(units, triple):
    objects = []
    for unit in units:
        sections, undefined = assemble_file(emit_self_asm(unit[1], triple))
        objects.append(NativeObject.from_sections(sections, undefined=undefined))
    obj = prepare_executable_object(objects)
    if any(section.sectname.startswith("__thread") for section in obj.sections):
        raise ValueError("owned host loader does not yet register native TLS images")
    page = mmap.PAGESIZE
    offsets = []
    code_ranges = []
    size = 0
    for section in obj.sections:
        alignment = max(page, 1 << section.align_log2)
        size = (size + alignment - 1) & -alignment
        offsets.append(size)
        span = (section.vm_size + page - 1) & -page
        if section.flags & spec.S_ATTR_PURE_INSTRUCTIONS and span:
            code_ranges.append((size, span))
        size += span
    undefined = [index for index, symbol in enumerate(obj.symbols) if symbol.section_index == 0]
    stub_offset = size
    stub_span = ((len(undefined) * 16 + page - 1) & -page) if undefined else 0
    size += stub_span
    got_offset = size
    got_span = (len(obj.symbols) * 8 + page - 1) & -page
    size += got_span
    memory = mmap.mmap(-1, max(size, page), flags=mmap.MAP_PRIVATE | mmap.MAP_ANON,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE)
    view = ctypes.c_char.from_buffer(memory)
    base = ctypes.addressof(view)
    del view
    library = ctypes.CDLL(None, use_errno=True)
    dlsym = library.dlsym
    dlsym.argtypes = (ctypes.c_void_p, ctypes.c_char_p)
    dlsym.restype = ctypes.c_void_p
    addresses = []
    stubs = {}
    try:
        for section, offset in zip(obj.sections, offsets):
            if section.data:
                memory[offset:offset + len(section.data)] = section.data
        for index, symbol in enumerate(obj.symbols):
            if symbol.section_index:
                address = base + offsets[symbol.section_index - 1] + symbol.offset
            else:
                name = symbol.name[1:] if symbol.name.startswith("_") else symbol.name
                address = dlsym(library._handle, name.encode("utf-8"))
                if not address:
                    raise ValueError("undefined native C symbol: " + symbol.name)
                stub = stub_offset + len(stubs) * 16
                # ldr x16, literal +8; br x16; absolute platform address.
                struct.pack_into("<IIQ", memory, stub, 0x58000050, 0xD61F0200, address)
                stubs[index] = base + stub
            addresses.append(address)
            struct.pack_into("<Q", memory, got_offset + index * 8, address)
        for section_index, section in enumerate(obj.sections):
            for relocation in section.relocations:
                at = offsets[section_index] + relocation.offset
                pc = base + at
                if relocation.symbol_index is not None:
                    target = addresses[relocation.symbol_index]
                elif relocation.target_section_index is not None:
                    target = base + offsets[relocation.target_section_index - 1] + (relocation.target_offset or 0)
                else:
                    raise ValueError("owned host relocation has no target")
                target += relocation.addend
                kind = relocation.type
                if kind == spec.ARM64_RELOC_UNSIGNED:
                    width = 1 << relocation.length
                    if relocation.minuend_index is not None:
                        target -= addresses[relocation.minuend_index]
                    original = int.from_bytes(memory[at:at + width], "little")
                    memory[at:at + width] = ((original + target) & ((1 << (width * 8)) - 1)).to_bytes(width, "little")
                    continue
                word = struct.unpack_from("<I", memory, at)[0]
                if kind == spec.ARM64_RELOC_BRANCH26:
                    target = stubs.get(relocation.symbol_index, target)
                    word = _branch26(word, target - pc)
                elif kind in (spec.ARM64_RELOC_PAGE21, spec.ARM64_RELOC_GOT_LOAD_PAGE21):
                    if kind == spec.ARM64_RELOC_GOT_LOAD_PAGE21:
                        target = base + got_offset + relocation.symbol_index * 8
                    delta = (target >> 12) - (pc >> 12)
                    if not -(1 << 20) <= delta < (1 << 20):
                        raise ValueError("owned host PAGE21 target is out of range")
                    word = _adrp_imm(word, delta)
                elif kind in (spec.ARM64_RELOC_PAGEOFF12, spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12):
                    if kind == spec.ARM64_RELOC_GOT_LOAD_PAGEOFF12:
                        target = base + got_offset + relocation.symbol_index * 8
                    word = _pageoff12(word, target & 4095)
                else:
                    raise ValueError("unsupported owned host relocation: " + str(kind))
                struct.pack_into("<I", memory, at, word)
        if stub_span:
            code_ranges.append((stub_offset, stub_span))
        invalidate = library.sys_icache_invalidate
        invalidate.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        invalidate.restype = None
        protect = library.mprotect
        protect.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        protect.restype = ctypes.c_int
        for offset, span in code_ranges:
            invalidate(base + offset, span)
            if protect(base + offset, span, mmap.PROT_READ | mmap.PROT_EXEC) != 0:
                raise OSError(ctypes.get_errno(), "owned host executable mapping failed")
        for index, section in enumerate(obj.sections):
            if section.sectname == "__mod_init_func":
                for at in range(0, section.vm_size, 8):
                    address = struct.unpack_from("<Q", memory, offsets[index] + at)[0]
                    ctypes.CFUNCTYPE(None)(address)()
        symbols = {symbol.name: addresses[index] for index, symbol in enumerate(obj.symbols)
                   if symbol.section_index}
        return memory, symbols
    except BaseException:
        memory.close()
        raise


def call_function(evaluator, units, triple, entry, function, args, descriptor, unsigned=False, base_dir=None):
    from pcc.frontends.c.evaluator.c_evaluator import get_c_type_from_serialized_ir

    memory, symbols = load_functions(units, triple)
    try:
        return _call_mapped_function(evaluator, memory, symbols, entry, function, args,
                                     descriptor, unsigned, base_dir)
    except BaseException:
        memory.close()
        raise


def _call_mapped_function(evaluator, memory, symbols, entry, function, args,
                          descriptor, unsigned, base_dir):
    from pcc.frontends.c.evaluator.c_evaluator import get_c_type_from_serialized_ir

    name = "_" + entry
    if name not in symbols:
        memory.close()
        raise ValueError("owned host entry was not emitted: " + entry)
    integer_types = {1: ctypes.c_bool, 8: ctypes.c_int8, 16: ctypes.c_int16,
                     32: ctypes.c_int32, 64: ctypes.c_int64}
    def scalar(ty):
        if ty.kind == "int":
            return integer_types[ty.width]
        if ty.kind == "fp":
            return ctypes.c_float if ty.width == 32 else ctypes.c_double
        if ty.kind == "ptr":
            return ctypes.c_void_p
        return None
    result_type = get_c_type_from_serialized_ir(descriptor) if descriptor else scalar(function.ret_type)
    if unsigned and function.ret_type.kind == "int":
        result_type = {8: ctypes.c_uint8, 16: ctypes.c_uint16, 32: ctypes.c_uint32, 64: ctypes.c_uint64}[function.ret_type.width]
    prototype = ctypes.CFUNCTYPE(result_type, *[scalar(arg.type) for arg in function.args])
    buffers = []
    supplied = []
    for value, parameter in zip(args, function.args):
        if parameter.type.kind == "ptr" and isinstance(value, (str, bytes)):
            buffer = ctypes.create_string_buffer(value.encode("utf-8") if isinstance(value, str) else value)
            buffers.append(buffer)
            value = ctypes.cast(buffer, ctypes.c_void_p)
        supplied.append(value)
    native = prototype(symbols[name])
    with _CALL_LOCK:
        previous_directory = os.getcwd()
        try:
            if base_dir:
                os.chdir(base_dir)
            result = native(*supplied)
        finally:
            if base_dir:
                os.chdir(previous_directory)
    module_owner = (memory, tuple(buffers))
    input_owners = []
    pointer_arguments = False
    for value, parameter in zip(args, function.args):
        if parameter.type.kind != "ptr" or value is None:
            continue
        pointer_arguments = True
        if isinstance(value, (str, bytes)):
            continue
        parent = getattr(value, "_obj", value)
        input_owners.append(parent)
        try:
            owners = getattr(parent, "_pcc_owned_modules", None)
            if owners is None:
                owners = []
                parent._pcc_owned_modules = owners
            owners.append(module_owner)
        except (AttributeError, TypeError):
            _OPAQUE_POINTER_OWNERS.append(module_owner)
    if pointer_arguments:
        # C can publish a module address through an out-parameter even when
        # its formal return type is scalar. Keep that image with caller data.
        evaluator._owned_host_images.append(module_owner)
    if result is not None and function.ret_type.kind == "ptr" and descriptor and len(descriptor) > 1 and descriptor[1] not in (None, ("void",)):
        result._pcc_owned_memory = memory
        result._pcc_argument_owners = tuple(buffers) + tuple(input_owners)
    else:
        if function.ret_type.kind == "ptr":
            _OPAQUE_POINTER_OWNERS.append((memory, tuple(buffers), tuple(input_owners)))
        elif not pointer_arguments:
            memory.close()
    return result
