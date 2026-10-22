"""Module naming helpers for Layer-1 codegen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir


_VOID = ir.VoidType()


def module_symbol_suffix(name: str) -> str:
    """Map a module name to the identifier fragment its symbols are built on.

    Not injective, and deliberately left that way for now: ``a.b``, ``a-b``,
    ``a+b`` and ``a b`` all yield ``a_b``.  The suffix is the only namespace
    separator in ``user_<mod>_<name>``, ``.class.<mod>.<Cls>``,
    ``.classattr.<mod>.<Cls>.<attr>``, ``_pcc_py_module_init_<mod>`` and
    ``__pcc_lru_cache_<mod>_<fn>``, so two modules in one multi-file compile
    whose names differ only in punctuation emit the same class global and the
    same module-init function.  ``class_gen`` then early-returns on the
    existing ``ir.Function`` and the second module silently reuses the
    first's.

    Making it injective means appending a digest, which renames symbols for
    every module whose name carries a path separator -- that is most of them
    under a path-derived name -- and so changes runtime archive members and
    compile-cache identity.  That is a deliberate migration, not a cleanup.
    """
    name = name.replace(".", "_").replace("-", "_")
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
    if not name.strip(alphabet):
        return name
    parts = []
    for character in name:
        parts.append(character if character in alphabet else "_")
    return "".join(parts)


class ModuleNameLoweringMixin:
    def _module_symbol_suffix(self, module_name: Optional[str] = None) -> str:
        name = module_name or self.module.name or "mod"
        return module_symbol_suffix(name)

    def _emit_module_teardown_call(self, module_name: Optional[str] = None) -> None:
        fini_name = self._module_teardown_name(module_name)
        existing = self.module.globals.get(fini_name)
        if existing is None:
            fini_fn = ir.Function(
                self.module,
                ir.FunctionType(_VOID, []),
                name=fini_name,
            )
            fini_fn.linkage = "external"
        else:
            fini_fn = existing
        self.builder.call(fini_fn, [])

    def _module_teardown_name(self, module_name: Optional[str] = None) -> str:
        return f"_pcc_py_module_fini_{self._module_symbol_suffix(module_name)}"
