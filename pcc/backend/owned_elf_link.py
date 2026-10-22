"""In-process Linux linker used by host pcc and native pcc stages."""

import os
import hashlib

from .elf_x86_64 import (
    EM_AARCH64, ElfError, _link_static_executable_image, parse_static_executable,
)
from .owned_elf_inputs import ElfInputStore, file_sha256
from .macho_internal_inputs import read_internal_input_manifest
from .self_backend_target_match import is_aarch64_linux_triple, is_x86_64_linux_triple



_IMAGE_CHUNK_BYTES = 1024 * 1024


def _image_chunks(image: bytearray):
    # Native file.write and hashlib each snapshot bytearray inputs. Publish
    # bounded immutable copies so both APIs retain bytes without another copy.
    for offset in range(0, len(image), _IMAGE_CHUNK_BYTES):
        yield bytes(image[offset:offset + _IMAGE_CHUNK_BYTES])


def _write_image_chunks(stream, image: bytearray) -> None:
    for chunk in _image_chunks(image):
        position = 0
        while position < len(chunk):
            count = stream.write(chunk[position:])
            if count is None or count <= 0 or count > len(chunk) - position:
                raise OSError("owned ELF image write made invalid progress")
            position += count


def _image_sha256(image: bytearray) -> str:
    digest = hashlib.sha256()
    for chunk in _image_chunks(image):
        digest.update(chunk)
    return digest.hexdigest()


def assemble(text: str, target: str, *, phase_timing=None):
    # Keep each lazy import and its call in the same target branch. Native
    # lowering must not merge different providers under one callable name.
    if is_aarch64_linux_triple(target):
        from .arm64_elf_driver import assemble_file as assemble_aarch64
        return assemble_aarch64(text)
    elif is_x86_64_linux_triple(target):
        from .x86_64_asm_driver import assemble_file as assemble_x86_64
        if phase_timing is not None:
            return assemble_x86_64(text, phase_timing=phase_timing)
        return assemble_x86_64(text)
    else:
        raise ElfError("unsupported owned ELF target: " + target)


def _thread_pointer_object(target: str):
    if is_aarch64_linux_triple(target):
        text = """.section __TEXT,__text,regular,pure_instructions
.globl pcc_linux_set_thread_pointer
pcc_linux_set_thread_pointer:
  msr tpidr_el0, x0
  mov x0, #0
  ret
"""
    else:
        text = """.intel_syntax noprefix
.text
.globl pcc_linux_set_thread_pointer
.type pcc_linux_set_thread_pointer, @function
pcc_linux_set_thread_pointer:
  mov rsi, rdi
  mov edi, 4098
  mov eax, 158
  syscall
  ret
.size pcc_linux_set_thread_pointer, .-pcc_linux_set_thread_pointer
"""
    return assemble(text, target)


def link_inputs(*, target: str, output: str, assembly=(), objects=(),
                archives=(), manifest: str = "", entry: str = "_start",
                map_path: str = "", coordinator_timing=None) -> None:
    if not (is_aarch64_linux_triple(target) or is_x86_64_linux_triple(target)):
        raise ElfError("unsupported owned ELF target: " + target)
    archives = list(archives)
    if manifest and (assembly or objects):
        raise ElfError("manifest cannot be mixed with direct internal inputs")
    manifest_hash = file_sha256(manifest) if manifest else ""
    ordered = read_internal_input_manifest(manifest) if manifest else (
        [("ASM", path) for path in assembly] + [("PCO", path) for path in objects]
    )
    if map_path:
        protected = [output, output + ".pcc-link.tmp", manifest] + [path for _kind, path in ordered] + archives
        if any(path and os.path.realpath(candidate) == os.path.realpath(path)
               for candidate in (map_path, map_path + ".pcc-link.tmp")
               for path in protected):
            raise ElfError("link map output aliases an input or executable")
    protected_inputs = [manifest] + [path for _kind, path in ordered] + archives
    if any(path and os.path.realpath(candidate) == os.path.realpath(path)
           for candidate in (output, output + ".pcc-link.tmp")
           for path in protected_inputs):
        raise ElfError("link output aliases an input")
    stage_started = 0
    if coordinator_timing is not None:
        stage_started = coordinator_timing.begin("elf_stage_inputs")
    store = ElfInputStore(target, archives)
    inputs = [store.stage(path, kind=kind) for kind, path in ordered]
    if not inputs:
        raise ElfError("owned ELF link requires an input")
    expected = EM_AARCH64 if is_aarch64_linux_triple(target) else 62
    if any(obj.machine != expected for obj in inputs):
        raise ElfError("input machine differs from selected ELF target")
    inputs.append(_thread_pointer_object(target))
    from .linux_thread_intrinsics import assembly as thread_assembly
    inputs.append(assemble(thread_assembly(expected == EM_AARCH64), target))
    if coordinator_timing is not None:
        coordinator_timing.complete("elf_stage_inputs", stage_started)
    selections = [] if map_path else None
    image_started = 0
    if coordinator_timing is not None:
        image_started = coordinator_timing.begin("elf_link_image")
    try:
        if coordinator_timing is not None:
            image = _link_static_executable_image(
                inputs, entry=entry, archive_selections=selections, _input_store=store,
                coordinator_timing=coordinator_timing,
            )
        else:
            image = _link_static_executable_image(
                inputs, entry=entry, archive_selections=selections, _input_store=store,
            )
        if coordinator_timing is not None:
            coordinator_timing.complete("elf_link_image", image_started)
    finally:
        verify_started = 0
        if coordinator_timing is not None:
            verify_started = coordinator_timing.begin("elf_verify_inputs")
        store.clear()
    store.verify()
    if manifest and file_sha256(manifest) != manifest_hash:
        raise ElfError("staged ELF manifest identity changed: " + manifest)
    parse_static_executable(image)
    if coordinator_timing is not None:
        coordinator_timing.complete("elf_verify_inputs", verify_started)
    map_text = ""
    if map_path:
        # These names come from the very selection that produced the image.
        # Preserve the actual parent archive identity; never invent a C-only
        # archive alias or manufacture a host-linker map.
        if any(store.duplicate_member_names):
            raise ElfError("link map cannot identify duplicate archive member names")
        lines = ["# pcc owned ELF link map v1", "# target " + target, "# entry " + entry,
                 "# image sha256=" + _image_sha256(image)]
        for index, member in selections:
            lines.append(str(archives[index]) + "(" + member + ")"
                         + " archive_sha256=" + store.archive_hashes[index]
                         + " member_sha256=" + store.member_hashes[index][member])
        map_text = "\n".join(lines) + "\n"
    temporary = output + ".pcc-link.tmp"
    write_started = 0
    if coordinator_timing is not None:
        write_started = coordinator_timing.begin("elf_write_image")
    try:
        with open(temporary, "wb") as stream:
            _write_image_chunks(stream, image)
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
        if map_path:
            map_temporary = map_path + ".pcc-link.tmp"
            try:
                with open(map_temporary, "w", encoding="utf-8") as stream:
                    stream.write(map_text)
                os.replace(map_temporary, map_path)
            finally:
                if os.path.exists(map_temporary):
                    os.unlink(map_temporary)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if coordinator_timing is not None:
        coordinator_timing.complete("elf_write_image", write_started)
