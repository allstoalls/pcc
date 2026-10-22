"""CPython import fallback state helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import Attr, DynType, Name

_I8 = ir.IntType(8)
_CSTR = _I8.as_pointer()


def live_import_global_slot(host, name):
    """Return a managed import's staging slot; its dictionary owns the binding.

    Runtime ports and freestanding compile-time imports have no executing
    Python module namespace. Their static ABI projections remain unchanged.
    """
    if getattr(host, "_runtime_port_module", False) or getattr(host, "_freestanding_module", False):
        return None
    slot = getattr(host, "_native_extension_module_env", {}).get(name)
    entry = getattr(host, "_module_globals", {}).get(name)
    if slot is not None and entry is not None and entry[0] is slot:
        return slot
    return None


def live_import_name_slot(host, name):
    slot = live_import_global_slot(host, name)
    local = host.env.get(name)
    if local is not None and local[0] is not slot:
        return None
    return slot


def retire_live_import_staging(host, name, unpin=True):
    staging = live_import_global_slot(host, name)
    if staging is None:
        return
    # An inferred import can reuse a scalar ABI projection allocated before
    # its live dictionary binding was discovered. Publication boxes that
    # value separately; this projection never acquired a GC owner or pin.
    # Keep its ABI contents and retire only an actual object staging slot.
    if not isinstance(staging.value_type, ir.PointerType):
        return
    if unpin:
        current = host.builder.load(staging, name=host._fresh("import.publish.current"))
        host._gc_unpin(current)
    host.builder.call(
        host.runtime["pcc_gc_store_root"],
        [host._as_gc_ptr(staging), ir.Constant(_CSTR, None)],
    )


def live_import_expr_binding(host, expr):
    """A real imported receiver cannot use historical provider metadata."""
    while isinstance(expr, Attr):
        expr = expr.obj
    if not isinstance(expr, Name):
        return False
    # A lexical local shadow is also a real receiver, not the provider alias.
    return live_import_global_slot(host, expr.ident) is not None


class CpyImportStateMixin:
    def _cpy_module_global(self, local_name: str) -> ir.GlobalVariable:
        """Return (or create) the module-level ``i8*`` global that
        stores the imported CPython ``PyObject *``. Shared across
        functions so a user's ``main()`` can read a module bound by a
        top-level ``import`` statement."""
        gname = f".cpy.modref.{local_name}"
        existing = self.module.globals.get(gname)
        if isinstance(existing, ir.GlobalVariable):
            return existing
        g = ir.GlobalVariable(self.module, _CSTR, name=gname)
        g.linkage = "internal"
        g.initializer = ir.Constant(_CSTR, None)
        return g

    def _cpy_modules(self) -> dict:
        """Module-wide map of imported local name → global variable."""
        if not hasattr(self, "_cpy_module_env"):
            self._cpy_module_env = {}
        return self._cpy_module_env

    def _native_extension_module_global(self, local_name: str) -> ir.GlobalVariable:
        """Share registered source-write staging storage with managed imports.

        The old private modref globals were neither registered nor updated by
        later Python assignments. Executed writes publish into the live module
        dictionary and then retire the staging owner.
        """
        slot, _declared = self._ensure_module_global_name(local_name, DynType(name="dyn"))
        if getattr(self, "_module_del_target_names", None) is None:
            self._module_del_target_names = set()
        self._module_del_target_names.add(local_name)
        return slot

    def _native_extension_modules(self) -> dict:
        if not hasattr(self, "_native_extension_module_env"):
            self._native_extension_module_env = {}
        return self._native_extension_module_env

    def _native_extension_star_modules(self) -> dict[str, ir.GlobalVariable]:
        if not hasattr(self, "_native_extension_star_module_env"):
            self._native_extension_star_module_env = {}
        return self._native_extension_star_module_env

    def _native_extension_star_module_global(
        self, module_name: str
    ) -> ir.GlobalVariable:
        """Keep only a scalar discovery marker for dynamic star-name lookup.

        The executing star import copies into the live namespace while its
        receiver has a temporary owner. It retains no hidden module pointer.
        """
        star_modules = self._native_extension_star_modules()
        gv = star_modules.get(module_name)
        if gv is not None:
            return gv
        gv = ir.GlobalVariable(
            self.module, _I8, name=".pcc.ext.star.imported." + module_name,
        )
        gv.linkage = "internal"
        gv.initializer = ir.Constant(_I8, 0)
        star_modules[module_name] = gv
        return gv

    def _load_from_native_extension_star_imports(self, name: str) -> Optional[ir.Value]:
        if not getattr(self, "_native_extension_star_module_env", {}):
            return None
        module_name = self.ast_module.name or "__main__"
        module_name_ptr = self._ptr_to_cstr(
            self._cstr_global(module_name, f".pcc.ext.star.module.{module_name}")
        )
        attr_ptr = self._ptr_to_cstr(
            self._cstr_global(name, f".pcc.ext.star.attr.{name}")
        )
        return self.builder.call(
            self.runtime["py_module_attr_get"],
            [module_name_ptr, attr_ptr],
            name=self._fresh(f"pcc.ext.star.{name}"),
        )

    def _cpy_star_modules(self) -> dict[str, ir.GlobalVariable]:
        """Globals storing modules imported via ``from x import *``."""
        if not hasattr(self, "_cpy_star_module_env"):
            self._cpy_star_module_env = {}
        return self._cpy_star_module_env

    def _cpy_star_module_global(self, module_name: str) -> ir.GlobalVariable:
        star_modules = self._cpy_star_modules()
        gv = star_modules.get(module_name)
        if gv is not None:
            return gv
        gv = self._cpy_module_global(f"starimport.{module_name}")
        star_modules[module_name] = gv
        return gv

    def _load_from_cpy_star_imports(self, name: str) -> Optional[ir.Value]:
        """Resolve an otherwise-unbound name from a prior star import."""
        star_modules = getattr(self, "_cpy_star_module_env", {})
        if not star_modules:
            return None
        attr_ptr = self._ptr_to_cstr(self._cstr_global(name, f".cpy.star.attr.{name}"))
        values = tuple(star_modules.values())
        idx = len(values) - 1
        gv = values[idx]
        mod_val = self.builder.load(gv, name=self._fresh(f"cpy.star.mod.{idx}"))
        val = self.builder.call(
            self.runtime["py_cpy_getattr"],
            [mod_val, attr_ptr],
            name=self._fresh(f"cpy.star.{name}"),
        )
        return self._mark_owned_cpy_value(val)

    def _ensure_cpy_init(self) -> None:
        """Emit a one-time ``py_cpy_ensure_init()`` in the current
        function. Idempotent both in IR (py_cpy_ensure_init's atomic
        guard) and in emission (we only emit it once per function
        compilation)."""
        if not hasattr(self, "_cpy_init_emitted_fns"):
            self._cpy_init_emitted_fns = set()
        fn_id = id(self.current_function)
        if fn_id in self._cpy_init_emitted_fns:
            return
        self.builder.call(self.runtime["py_cpy_ensure_init"], [])
        self._cpy_init_emitted_fns.add(fn_id)

    # -- With-statement (context manager) -----------------------------
