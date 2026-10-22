"""String/global constant helpers for Layer-1 Python codegen."""

from __future__ import annotations

from pcc.ir.compat import ir

_I8 = ir.IntType(8)
_I32 = ir.IntType(32)


class StringGlobalsLoweringMixin:
    def _utf8_byte_values(self, payload: str) -> list[int]:
        """Return UTF-8 byte values for ``payload`` without a bytes object.

        The self-hosted compiler cannot depend on CPython ``bytes`` or
        ``str.encode`` here: these globals are emitted while compiling
        user programs. Keep the helper in ordinary Python so CPython
        and pcc-Python execute the same code path.
        """
        out: list[int] = []
        i = 0
        n = len(payload)
        while i < n:
            cp = ord(payload[i])
            if cp <= 127:
                out.append(cp)
            elif cp <= 2047:
                out.append(192 | (cp >> 6))
                out.append(128 | (cp & 63))
            elif cp <= 65535:
                out.append(224 | (cp >> 12))
                out.append(128 | ((cp >> 6) & 63))
                out.append(128 | (cp & 63))
            else:
                out.append(240 | (cp >> 18))
                out.append(128 | ((cp >> 12) & 63))
                out.append(128 | ((cp >> 6) & 63))
                out.append(128 | (cp & 63))
            i += 1
        return out

    def _cstr_literal(self, payload: str) -> tuple[ir.GlobalVariable, int]:
        """Intern a UTF-8 byte array as an internal global.

        Returns ``(gv, byte_len)`` where ``byte_len`` excludes the
        trailing NUL. Emitted globals are named ``.pystr.<N>`` per the
        L2 convention in the task brief.
        """
        data = self._utf8_byte_values(payload)
        existing = self._str_pool.get(payload)
        if existing is not None:
            # Array length minus the NUL terminator.
            arr_ty = existing.type.pointee
            return existing, arr_ty.count - 1
        self._str_counter += 1
        name = ".pystr." + str(self._str_counter)
        body = data + [0]
        arr_ty = ir.ArrayType(_I8, len(body))
        gv = ir.GlobalVariable(self.module, arr_ty, name=name)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(arr_ty, body)
        self._str_pool[payload] = gv
        return gv, len(data)

    def _attr_name_ptr(self, name: str) -> ir.Value:
        """Return an i8* pointing at a NUL-terminated attribute name.

        These globals are short-lived (attribute-access use only) and
        intentionally distinct from :meth:`_cstr_literal` so a later
        optimiser can fold them if it wishes.
        """
        # Contextual mixin ``self`` parameters are not yet described as an
        # L1CodeGen object in the native function ABI, so they are not entered
        # as borrowed GC roots.  Snapshot every heap object needed below into
        # typed locals before the first allocation and never reload through a
        # potentially relocated ``self`` pointer later in this routine.
        stable_name: str = name
        attr_pool: dict[str, ir.GlobalVariable] = self._attr_pool
        module: ir.Module = self.module
        name_bytes: list[int] = self._utf8_byte_values(stable_name)
        existing = attr_pool.get(stable_name)
        if existing is None:
            data = name_bytes + [0]
            arr_ty = ir.ArrayType(_I8, len(data))
            sym = ".pyattr."
            sym = sym + stable_name
            # Multiple distinct attrs may share a name; disambiguate.
            if sym in module.globals:
                attr_count_text = str(len(attr_pool))
                sym = ".pyattr."
                sym = sym + stable_name
                sym = sym + "."
                sym = sym + attr_count_text
            gv = ir.GlobalVariable(module, arr_ty, name=sym)
            gv.linkage = "internal"
            gv.global_constant = True
            gv.initializer = ir.Constant(arr_ty, data)
            attr_pool[stable_name] = gv
            existing = gv
            name_text = sym
        else:
            typed_existing: ir.GlobalVariable = existing
            name_text = typed_existing.name
        # Keep each rendered component in a named local.  In pcc1-native
        # code, inline ``str(existing.*)`` results are otherwise unrooted
        # across the allocating concatenation chain and can become NULL late
        # in a large module-top initializer.
        array_count = len(name_bytes) + 1
        array_count_text = str(array_count)
        value_type_text = "["
        value_type_text = value_type_text + array_count_text
        value_type_text = value_type_text + " x i8]"
        # String globals are always addrspace-0 opaque pointers.
        pointer_type_text = "ptr"
        expr = "getelementptr inbounds ("
        expr = expr + value_type_text
        expr = expr + ", "
        expr = expr + pointer_type_text
        expr = expr + " @"
        expr = expr + name_text
        expr = expr + ", i32 0, i32 0)"
        return ir.Value(ir.PointerType(_I8), expr)

    def _cstr_global(self, payload: str, name: str) -> ir.GlobalVariable:
        # Filenames and dynamic attribute/module names may contain characters
        # that are not backend identifiers. Keep them in the UTF-8 payload;
        # a pooled numeric label gives the private global a safe unique name.
        if (
            not name
            or name[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_.$"
            or name.strip("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.$")
        ):
            return self._pooled_cstr_global(payload, ".cstr.symbol")
        data = self._utf8_byte_values(payload) + [0]
        arr_ty = ir.ArrayType(_I8, len(data))
        existing = self.module.globals.get(name)
        if isinstance(existing, ir.GlobalVariable):
            return existing
        gv = ir.GlobalVariable(self.module, arr_ty, name=name)
        gv.linkage = "internal"
        gv.global_constant = True
        gv.initializer = ir.Constant(arr_ty, data)
        return gv

    def _pooled_cstr_global(
        self,
        payload: str,
        prefix: str = ".cstr",
    ) -> ir.GlobalVariable:
        existing = self._cstr_pool.get(payload)
        if existing is not None:
            return existing
        self._cstr_counter += 1
        name = str(prefix) + "." + str(self._cstr_counter)
        while name in self.module.globals:
            self._cstr_counter += 1
            name = str(prefix) + "." + str(self._cstr_counter)
        gv = self._cstr_global(payload, name)
        self._cstr_pool[payload] = gv
        return gv

    def _pooled_cstr_ptr(
        self,
        payload: str,
        prefix: str = ".cstr",
    ) -> ir.Value:
        # This private pool contains only byte arrays created by
        # _pooled_cstr_global in the default address space.  Their first byte
        # has the same opaque-pointer address as the global itself.  Keep the
        # tracked i8 pointee without emitting a redundant SSA GEP per use.
        # Typed locals keep the returned global and reference live across
        # allocating IR constructors in the native compiler.
        gv: ir.GlobalVariable = self._pooled_cstr_global(payload, prefix)
        global_ref: str = str(gv)
        pointer_type: ir.PointerType = ir.PointerType(_I8)
        return ir.Value(pointer_type, global_ref)

    def _ptr_to_cstr(self, gv: ir.GlobalVariable) -> ir.Value:
        zero = ir.Constant(_I32, 0)
        return self.builder.gep(gv, [zero, zero], inbounds=True)

    def _get_fmt_int(self) -> ir.GlobalVariable:
        if self._fmt_int is None:
            self._fmt_int = self._cstr_global("%ld\n", ".fmt_int")
        return self._fmt_int

    def _get_fmt_float(self) -> ir.GlobalVariable:
        if self._fmt_float is None:
            # Use %g for a Python-ish default; this is NOT bit-for-bit
            # Python's repr and will be upgraded in Phase 2 when the
            # runtime lib is wired in for repr.
            self._fmt_float = self._cstr_global("%g\n", ".fmt_float")
        return self._fmt_float

    def _get_fmt_bool_true(self) -> ir.GlobalVariable:
        if self._fmt_bool_true is None:
            self._fmt_bool_true = self._cstr_global("True\n", ".fmt_true")
        return self._fmt_bool_true

    def _get_fmt_bool_false(self) -> ir.GlobalVariable:
        if self._fmt_bool_false is None:
            self._fmt_bool_false = self._cstr_global("False\n", ".fmt_false")
        return self._fmt_bool_false
