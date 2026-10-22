from __future__ import annotations

from pcc.frontends.c.c_libc_registry import LibcSignature


EXTRA_SIGNATURES: tuple[LibcSignature, ...] = (
    LibcSignature("calloc", "void*", ("size_t", "size_t"), "stdlib.h"),
    LibcSignature("realloc", "void*", ("void*", "size_t"), "stdlib.h"),
    LibcSignature("strcmp", "int", ("const char*", "const char*"), "string.h"),
    LibcSignature("memmove", "void*", ("void*", "const void*", "size_t"), "string.h"),
    LibcSignature("dlopen", "void*", ("const char*", "int"), "dlfcn.h"),
    LibcSignature("dlsym", "void*", ("void*", "const char*"), "dlfcn.h"),
)
