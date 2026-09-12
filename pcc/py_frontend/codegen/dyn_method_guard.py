"""Runtime type-tag guard for ``dyn``-receiver container fast paths.

``list``/``dict``/``set`` method fast paths on a ``dyn`` receiver are
selected by method *name* alone, because the static type says nothing.
That is sound only while no other object answers to the same name -- and
user classes routinely define ``update``, ``add``, ``copy`` and ``get``.
Without a guard the call reached ``py_set_update``/``py_dict_get`` with a
user instance, which those helpers ignore: the method body never ran and
nothing was raised, so the call silently evaporated.

``pcc/py_stdlib/hashlib.py`` is the worked example -- ``clone.update(...)``
in ``_SHA256.digest`` did nothing, and its ``while len(clone._buf) != 56``
padding loop then spun forever.

The list path already tested ``py_obj_type_tag`` before committing; this
mixin is that check, factored out so ``dict`` and ``set`` share it.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from pcc.llvm_capi.compat import ir

from ..py_ast import Attr, Call


_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()


class DynMethodGuardMixin:
    def _emit_dyn_container_method_with_tag_guard(
        self,
        expr: Call,
        type_tags: Sequence[int],
        emit_native: Callable[[ir.Value], Optional[ir.Value]],
        label: str,
    ) -> Optional[ir.Value]:
        """Run ``emit_native`` only when the receiver really is that container.

        Everything else -- a user class that happens to define the same
        method name included -- goes to generic dispatch, which finds the
        bound method through ``py_obj_getattr`` and calls it.
        """
        attr = expr.func
        assert isinstance(attr, Attr)
        if self.current_function is None:
            return None

        recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            return self._emit_cpy_method_call_src(
                recv,
                attr.name,
                expr.args,
                kwargs=expr.kwargs,
            )

        tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [recv],
            name=self._fresh(f"{label}.recv.tag"),
        )
        matches = None
        for type_tag in type_tags:
            is_tag = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, type_tag),
                name=self._fresh(f"{label}.recv.is{type_tag}"),
            )
            matches = (
                is_tag
                if matches is None
                else self.builder.or_(
                    matches, is_tag, name=self._fresh(f"{label}.recv.any")
                )
            )
        assert matches is not None

        fn = self.current_function
        native_bb = fn.append_basic_block(name=self._fresh(f"{label}.native"))
        generic_bb = fn.append_basic_block(name=self._fresh(f"{label}.generic"))
        done_bb = fn.append_basic_block(name=self._fresh(f"{label}.done"))
        self.builder.cbranch(matches, native_bb, generic_bb)

        self.builder.position_at_end(native_bb)
        native_result = emit_native(recv)
        if native_result is None:
            # The fast path declined this call shape after the branch was
            # already emitted (argument-count checks only -- none of them
            # lower an argument first, so nothing is evaluated twice).
            # Terminating the block with generic dispatch keeps the IR
            # well-formed and the semantics right.
            native_result = self._emit_generic_dyn_method_call_on_value(
                recv,
                attr.name,
                expr,
            )
        native_exit = self.builder.block
        self._gc_release_if_owned(recv, attr.obj)
        self.builder.branch(done_bb)

        self.builder.position_at_end(generic_bb)
        generic_result = self._emit_generic_dyn_method_call_on_value(
            recv,
            attr.name,
            expr,
        )
        generic_exit = self.builder.block
        self._gc_release_if_owned(recv, attr.obj)
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_CSTR, name=self._fresh(f"{label}.result"))
        result.add_incoming(native_result, native_exit)
        result.add_incoming(generic_result, generic_exit)
        return result
