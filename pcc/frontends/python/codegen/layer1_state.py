"""The shared-state surface every L1CodeGen mixin may rely on.

`L1CodeGen` composes 86 mixin classes over one `self` namespace. Until now the
state those mixins read from `self` was implicit: a mixin could reach for
`self.builder` or `self.current_function` with nothing declaring that the
attribute exists, who sets it, or when it is valid. This module makes that
surface explicit (ARCH-P3-LAYER1-STATE-PROTOCOL).

This is a declaration, not a mechanism: nothing enforces it at runtime, and
composing it changes no behavior. Its job is to give the mixins one place that
answers "what am I allowed to assume about `self`?", and to give reviewers a
diff to look at when that assumption set grows.

The attribute list is measured, not guessed — it is the set of non-underscore
`self.<attr>` reads across `codegen/*_lowering.py` and `codegen/native_*.py`,
ordered by how many read sites each has.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class L1CodeGenState(Protocol):
    """State an L1CodeGen mixin may read off ``self``.

    Attribute types are deliberately loose (``Any``): the point is to name the
    surface and its owner, not to re-type the IR layer. Where an attribute is
    only valid inside a phase, the docstring says so — reading it outside that
    phase is the bug this declaration is meant to make visible.
    """

    # --- IR emission targets (owned by the function-emission phase) ---
    builder: Any
    """Current IRBuilder. Only valid while emitting a function body."""

    module: Any
    """The owned IR module being built. Valid for the whole run."""

    runtime: Any
    """Mapping of runtime symbol name -> declared ir.Function."""

    current_function: Any
    """ir.Function being emitted, or None outside a function body."""

    current_func_def: Any
    """The FuncDef AST node behind ``current_function``, or None."""

    functions: Any
    """Emitted user functions by Python-level name."""

    # --- Source and scope context ---
    env: Any
    """Name -> (type, value) scope chain for the unit being compiled."""

    ast_module: Any
    """The Module AST node currently being lowered."""

    # --- Class lowering context (owned by class_gen) ---
    class_lowering: Any
    """The ClassLowering helper; owns class info, method defs, layouts."""

    current_class: Any
    """Class being emitted, or None."""

    current_method_kind: Any
    """"static" / "property_getter" / ... while emitting a method."""

    env_class_hint: Any
    env_class_object_hint: Any
    env_list_elem_class_hint: Any
    """Inference hints threaded through expression lowering."""

    # --- Statement context ---
    loop_stack: Any
    """Stack of (continue_block, break_block) for loop lowering."""

    # --- Mode flags ---
    ir_scaffold_mode: Any
    emit_cpy_main_exitcode: Any


SHARED_STATE_ATTRIBUTES = tuple(
    sorted(
        name
        for name in L1CodeGenState.__annotations__
        if not name.startswith("_")
    )
)
