"""Closed-world ``may_park`` analysis for pcc virtual threads.

The effect is intentionally closed-world and auditable.  A directly-bound
Python function is ``may_park`` when it calls one of the current-virtual-thread
suspension primitives, or when it directly calls another ``may_park``
function.  The whole-program context pass publishes that fact on native
sibling exports; each module then repeats the fixed point after closure
hoisting so the affected definitions use the existing heap-owned
generator/continuation frame contract.

Compiled sibling functions and method descriptors are resolved through native
closure metadata.  Local methods join the fixed point only when their receiver
class is statically concrete; unresolved dynamic dispatch remains an
open-world boundary and is never guessed resumable.
"""

from __future__ import annotations

from typing import Optional

from pcc.backend.self_backend_value_arena import CompilerIntArena

from ..py_ast import (
    Assign,
    Attr,
    AugAssign,
    BoolType,
    BytesType,
    Call,
    ClassDef,
    ClassType,
    DictType,
    FloatType,
    IntType,
    ListType,
    NoneType,
    SetType,
    StrType,
    TupleType,
    Delete,
    ExceptHandler,
    For,
    FuncDef,
    Global,
    Import,
    ImportFrom,
    Lambda,
    ListExpr,
    Module,
    Name,
    Nonlocal,
    TupleExpr,
    With,
)
from .hoist_analysis import _dataclass_field_names, _dataclass_field_value
from ..vthread_effect_summary_wire import (
    read_summary as _read_vthread_effect_summary_wire,
    write_summary as _write_vthread_effect_summary_wire,
)


_SUSPENSION_EXPORTS = (
    "continuation",
    "completed",
    "call",
    "yield_now",
    "join",
    "send",
    "recv",
    "select2",
    "sleep_current",
    "block_current_on_fd",
    "readable",
    "writable",
    "tcp_accept",
    "tcp_connect",
    "tcp_recv",
    "tcp_send_all",
)

# Name-resolution proof and effect classification are deliberately separate.
# Every callable exported by pcc.virtual_thread can be lowered through a
# from-import alias, but only _SUSPENSION_EXPORTS makes the current function a
# resumable continuation.
_VTHREAD_VALUE_EXPORTS = (
    "continuation",
    "completed",
    "continuation_factory",
    "spawn",
    "call",
    "join",
    "cancel",
    "mpsc",
    "oneshot",
    "sender_clone",
    "send",
    "recv",
    "close_sender",
    "close_receiver",
    "select2",
    "run",
    "run_until_idle",
    "carrier_pool_start",
    "carrier_pool_stop",
    "io_backend",
    "current",
    "yield_now",
    "sleep_current",
    "block_current_on_fd",
    "readable",
    "writable",
    "tcp_listen",
    "tcp_accept",
    "tcp_connect",
    "tcp_recv",
    "tcp_send_all",
    "tcp_close",
    "result",
    "exception",
    "outcome",
    "state",
    "sleep",
    "block_on_fd",
)

# Native condition/event/semaphore waits use the same continuation yield
# contract as virtual-thread primitives and are effect roots.  Plain
# ``Lock.acquire`` is intentionally not a root: lock-backed properties cannot
# expose a suspension ABI through Python's descriptor protocol.  Tiny internal
# gateway/accounting critical sections therefore remain explicit synchronous
# carrier-pin regions.  When a containing callable is already resumable (for
# example BodyStream.read_chunk because it waits on Event), native_threading
# still selects ``py_threading_lock_acquire_vthread`` and never pins on that
# path.  Keep the roots below aligned with the park-capable branches in
# NativeThreadingLoweringMixin.
_THREADING_SUSPENSION_METHODS = {
    "Event": frozenset(("wait",)),
    "Condition": frozenset(("wait",)),
    "Semaphore": frozenset(("acquire",)),
}

# These fields describe the semantic result or declaration type; they are not
# executable syntax children.  Walking them turns closed-world ClassType /
# FuncType metadata into a second, often enormous graph traversal for every
# function and method in a module.
_NON_SYNTAX_FIELDS = frozenset(("span", "ty", "annotation", "return_ty"))


def _resolve_import_from_module(module: Module, stmt: ImportFrom) -> str:
    """Resolve an ImportFrom without depending on the host pipeline module."""
    level = stmt.level or 0
    if level <= 0:
        return stmt.module or ""
    current = module.name or ""
    parts = current.split(".") if current else []
    source_file = (stmt.span.file or "").replace("\\", "/")
    is_package_init = source_file == "__init__.py" or source_file.endswith(
        "/__init__.py"
    )
    package_parts = parts if is_package_init else parts[:-1]
    up = level - 1
    if up > len(package_parts):
        return stmt.module or ""
    base = package_parts[: len(package_parts) - up]
    if stmt.module:
        return ".".join(base + stmt.module.split("."))
    return ".".join(base)


def _module_scope_imports(module: Module) -> list[object]:
    """Return imports reachable at module scope, excluding function bodies."""
    out: list[object] = []
    work = []
    for stmt in module.body:
        work.append(stmt)
    index = 0
    while index < len(work):
        node = work[index]
        index += 1
        if node is None:
            continue
        if isinstance(node, tuple):
            for item in node:
                work.append(item)
            continue
        if isinstance(node, (FuncDef, ClassDef)):
            continue
        if isinstance(node, (Import, ImportFrom)):
            out.append(node)
            continue
        for field_name in _dataclass_field_names(node):
            if field_name in _NON_SYNTAX_FIELDS:
                continue
            value = _dataclass_field_value(node, field_name, None)
            if isinstance(value, tuple):
                for item in value:
                    work.append(item)
            else:
                work.append(value)
    return out


def _export_function_target(
    native_exports: Optional[dict],
    module_name: str,
    export_name: str,
) -> Optional[tuple[str, str]]:
    if native_exports is None:
        return None
    info = native_exports.get(module_name, {}).get(export_name)
    if not isinstance(info, dict) or info.get("kind") != "function":
        return None
    # A semantic decorator replaces the public binding with an arbitrary
    # callable object.  It is deliberately outside the direct native ABI.
    if info.get("semantic_decorator"):
        return None
    return (
        str(info.get("owning_module", module_name)),
        str(info.get("export_name", export_name)),
    )


def _cross_module_bindings(
    module: Module,
    native_exports: Optional[dict],
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Return directly imported functions and compiled module aliases."""
    function_bindings: dict[str, tuple[str, str]] = {}
    module_bindings: dict[str, str] = {}
    if native_exports is None:
        return function_bindings, module_bindings
    for stmt in _module_scope_imports(module):
        if isinstance(stmt, Import):
            for imported_module, as_name in stmt.names:
                if imported_module not in native_exports:
                    continue
                if as_name is None and "." in imported_module:
                    # ``import a.b`` binds ``a``.  The codegen module-alias
                    # table resolves this dotted form separately; avoiding a
                    # guessed binding here keeps the analysis fail-closed.
                    continue
                module_bindings[as_name or imported_module] = imported_module
            continue
        resolved = _resolve_import_from_module(module, stmt)
        for imported_name, as_name in stmt.names:
            if imported_name == "*":
                continue
            local_name = as_name or imported_name
            target = _export_function_target(
                native_exports,
                resolved,
                imported_name,
            )
            if target is not None:
                function_bindings[local_name] = target
                continue
            submodule = resolved + "." + imported_name if resolved else imported_name
            if submodule in native_exports:
                module_bindings[local_name] = submodule
    return function_bindings, module_bindings


def _cross_module_class_bindings(
    module: Module,
    native_exports: Optional[dict],
) -> dict[str, tuple[str, str]]:
    bindings: dict[str, tuple[str, str]] = {}
    if native_exports is None:
        return bindings
    for stmt in _module_scope_imports(module):
        if not isinstance(stmt, ImportFrom):
            continue
        resolved = _resolve_import_from_module(module, stmt)
        exports = native_exports.get(resolved, {})
        for imported_name, as_name in stmt.names:
            info = exports.get(imported_name)
            if isinstance(info, dict) and info.get("kind") == "class":
                bindings[as_name or imported_name] = (resolved, imported_name)
    return bindings


def _export_target_is_may_park(
    native_exports: Optional[dict],
    target: tuple[str, str],
) -> bool:
    if native_exports is None:
        return False
    module_name, export_name = target
    info = native_exports.get(module_name, {}).get(export_name)
    return isinstance(info, dict) and bool(info.get("may_park", False))


def _module_function_defs(module: Module) -> list[FuncDef]:
    """Return module-scope function definitions in deterministic order.

    Nested functions have already been closure-hoisted before this analysis is
    called.  Class methods are collected separately so their lexical owner is
    part of the effect key.
    """
    out: list[FuncDef] = []
    work = []
    for stmt in module.body:
        work.append(stmt)
    index = 0
    while index < len(work):
        node = work[index]
        index += 1
        if isinstance(node, tuple):
            for item in node:
                work.append(item)
            continue
        if isinstance(node, FuncDef):
            out.append(node)
            continue
        if isinstance(node, ClassDef):
            continue
        for field_name in _dataclass_field_names(node):
            if field_name in _NON_SYNTAX_FIELDS:
                continue
            value = _dataclass_field_value(node, field_name, None)
            if isinstance(value, tuple):
                for item in value:
                    # Import ``names`` and With ``items`` are tuples too, but
                    # their scalar pairs have no dataclass fields and are
                    # harmless work-list entries.
                    work.append(item)
            else:
                work.append(value)
    return out


_BINDING_DYNAMIC = "dynamic"
_BINDING_VTHREAD_MODULE = "module:pcc.virtual_thread"
_BINDING_VTHREAD_VALUE_PREFIX = "value:pcc.virtual_thread."


def _record_binding_kind(
    bindings: dict[str, set[str]],
    name: str,
    kind: str,
) -> None:
    if not name:
        return
    kinds = bindings.get(name)
    if kinds is None:
        kinds = set()
        bindings[name] = kinds
    kinds.add(kind)


def _collect_binding_target_names(target, names: set[str]) -> None:
    if isinstance(target, Name):
        names.add(target.ident)
        return
    if isinstance(target, (TupleExpr, ListExpr)):
        for item in target.elems:
            _collect_binding_target_names(item, names)


def _function_scope_nodes(fd: FuncDef) -> list[object]:
    """Return nodes evaluated in ``fd`` without entering nested scopes."""
    nodes: list[object] = []
    work = list(fd.body)
    index = 0
    while index < len(work):
        node = work[index]
        index += 1
        if node is None:
            continue
        if isinstance(node, tuple):
            work.extend(node)
            continue
        nodes.append(node)
        if isinstance(node, (FuncDef, ClassDef, Lambda)):
            continue
        for field_name in _dataclass_field_names(node):
            if field_name in _NON_SYNTAX_FIELDS:
                continue
            value = _dataclass_field_value(node, field_name, None)
            if isinstance(value, tuple):
                work.extend(value)
            else:
                work.append(value)
    return nodes


def _function_lexical_binding_kinds(
    fd: FuncDef,
    scope_nodes: Optional[list[object]] = None,
) -> tuple[dict[str, set[str]], set[str], set[str]]:
    """Collect function-scope bindings using Python's lexical scope rule.

    A binding anywhere in a function shadows the module binding for the whole
    function.  Exact local virtual-thread imports remain provable only when no
    other binding source can replace the same name.
    """
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    global_names: set[str] = set()
    nonlocal_names: set[str] = set()
    for node in nodes:
        if isinstance(node, Global):
            global_names.update(node.names)
        elif isinstance(node, Nonlocal):
            nonlocal_names.update(node.names)

    bindings: dict[str, set[str]] = {}
    for arg in fd.args:
        _record_binding_kind(bindings, arg.name, _BINDING_DYNAMIC)

    for node in nodes:
        target_names: set[str] = set()
        if isinstance(node, Assign):
            for target in node.targets:
                _collect_binding_target_names(target, target_names)
        elif isinstance(node, AugAssign):
            _collect_binding_target_names(node.target, target_names)
        elif isinstance(node, For):
            _collect_binding_target_names(node.target, target_names)
        elif isinstance(node, With):
            for _context, target in node.items:
                if target is not None:
                    _collect_binding_target_names(target, target_names)
        elif isinstance(node, ExceptHandler):
            if node.name:
                target_names.add(node.name)
        elif isinstance(node, Delete):
            for target in node.targets:
                _collect_binding_target_names(target, target_names)
        elif isinstance(node, (FuncDef, ClassDef)):
            target_names.add(node.name)
        elif (
            isinstance(node, Call)
            and isinstance(node.func, Name)
            and node.func.ident in ("_walrus", "__walrus__")
            and len(node.args) == 2
        ):
            _collect_binding_target_names(node.args[0], target_names)
        for name in target_names:
            _record_binding_kind(bindings, name, _BINDING_DYNAMIC)

        if isinstance(node, Import):
            for module_name, as_name in node.names:
                local_name = as_name or module_name.split(".")[0]
                kind = _BINDING_DYNAMIC
                if module_name == "pcc.virtual_thread" and as_name is not None:
                    kind = _BINDING_VTHREAD_MODULE
                _record_binding_kind(bindings, local_name, kind)
        elif isinstance(node, ImportFrom):
            for imported_name, as_name in node.names:
                if imported_name == "*":
                    continue
                local_name = as_name or imported_name
                kind = _BINDING_DYNAMIC
                if (
                    node.level == 0
                    and node.module == "pcc.virtual_thread"
                    and imported_name in _VTHREAD_VALUE_EXPORTS
                ):
                    kind = _BINDING_VTHREAD_VALUE_PREFIX + imported_name
                _record_binding_kind(bindings, local_name, kind)
    return bindings, global_names, nonlocal_names


def _function_vthread_bindings(
    fd: FuncDef,
    module_aliases: dict[str, str],
    value_aliases: dict[str, str],
    scope_nodes: Optional[list[object]] = None,
) -> tuple[dict[str, str], dict[str, str], set[str], set[str], set[str]]:
    """Apply function lexical bindings to module-scope proven aliases.

    The returned blocked-name set also guards direct local/sibling function
    edges.  The final two sets identify virtual-thread spellings whose runtime
    value became ambiguous and therefore require a fail-closed boundary.
    """
    effective_modules = dict(module_aliases)
    effective_values = dict(value_aliases)
    blocked_names: set[str] = set()
    uncertain_modules: set[str] = set()
    uncertain_values: set[str] = set()
    bindings, global_names, nonlocal_names = _function_lexical_binding_kinds(
        fd,
        scope_nodes,
    )

    for name, kinds in bindings.items():
        if name in global_names:
            # A function-scope write declared global mutates a module binding in
            # source order.  This pass is intentionally not a flow analysis.
            effective_modules.pop(name, None)
            effective_values.pop(name, None)
            blocked_names.add(name)
            if name in module_aliases or _BINDING_VTHREAD_MODULE in kinds:
                uncertain_modules.add(name)
            if value_aliases.get(name) in _SUSPENSION_EXPORTS:
                uncertain_values.add(name)
            for kind in kinds:
                if (
                    kind.startswith(_BINDING_VTHREAD_VALUE_PREFIX)
                    and kind[len(_BINDING_VTHREAD_VALUE_PREFIX) :]
                    in _SUSPENSION_EXPORTS
                ):
                    uncertain_values.add(name)
            continue

        blocked_names.add(name)
        prior_module = name in effective_modules
        prior_value = effective_values.get(name)
        effective_modules.pop(name, None)
        effective_values.pop(name, None)
        if len(kinds) == 1 and _BINDING_VTHREAD_MODULE in kinds:
            effective_modules[name] = "pcc.virtual_thread"
            continue
        if len(kinds) == 1:
            only_kind = next(iter(kinds))
            if only_kind.startswith(_BINDING_VTHREAD_VALUE_PREFIX):
                effective_values[name] = only_kind[
                    len(_BINDING_VTHREAD_VALUE_PREFIX) :
                ]
                continue
        if prior_module or _BINDING_VTHREAD_MODULE in kinds:
            uncertain_modules.add(name)
        if prior_value in _SUSPENSION_EXPORTS:
            uncertain_values.add(name)
        for kind in kinds:
            if (
                kind.startswith(_BINDING_VTHREAD_VALUE_PREFIX)
                and kind[len(_BINDING_VTHREAD_VALUE_PREFIX) :]
                in _SUSPENSION_EXPORTS
            ):
                uncertain_values.add(name)

    for name in nonlocal_names:
        if name in effective_modules:
            uncertain_modules.add(name)
        if effective_values.get(name) in _SUSPENSION_EXPORTS:
            uncertain_values.add(name)
        effective_modules.pop(name, None)
        effective_values.pop(name, None)
        blocked_names.add(name)
    return (
        effective_modules,
        effective_values,
        blocked_names,
        uncertain_modules,
        uncertain_values,
    )


def _vthread_import_aliases(
    module: Module,
) -> tuple[dict[str, str], dict[str, str]]:
    """Collect proven module-scope virtual-thread import bindings."""
    module_aliases: dict[str, str] = {}
    value_aliases: dict[str, str] = {}
    work = []
    for stmt in module.body:
        work.append(stmt)
    index = 0
    while index < len(work):
        node = work[index]
        index += 1
        if isinstance(node, tuple):
            for item in node:
                work.append(item)
            continue
        if isinstance(node, (FuncDef, ClassDef)):
            continue
        if isinstance(node, Import):
            for module_name, as_name in node.names:
                if module_name == "pcc.virtual_thread":
                    # ``import pcc.virtual_thread`` binds ``pcc`` and needs
                    # nested attribute resolution, which the current native
                    # virtual-thread lowering does not claim.  The explicit
                    # alias form is closed and supported.
                    if as_name is not None:
                        module_aliases[as_name] = module_name
            continue
        if isinstance(node, ImportFrom):
            if node.level == 0 and node.module == "pcc.virtual_thread":
                for imported_name, as_name in node.names:
                    if imported_name in _VTHREAD_VALUE_EXPORTS:
                        value_aliases[as_name or imported_name] = imported_name
            continue
        for field_name in _dataclass_field_names(node):
            if field_name in _NON_SYNTAX_FIELDS:
                continue
            value = _dataclass_field_value(node, field_name, None)
            if isinstance(value, tuple):
                for item in value:
                    work.append(item)
            else:
                work.append(value)
    return module_aliases, value_aliases


def _suspension_call_export(
    node,
    module_aliases: dict[str, str],
    value_aliases: dict[str, str],
) -> Optional[str]:
    if not isinstance(node, Call):
        return None
    fn = node.func
    if isinstance(fn, Name):
        export = value_aliases.get(fn.ident)
        if export in _SUSPENSION_EXPORTS:
            return export
        return None
    if not isinstance(fn, Attr) or not isinstance(fn.obj, Name):
        return None
    if (
        module_aliases.get(fn.obj.ident) == "pcc.virtual_thread"
        and fn.name in _SUSPENSION_EXPORTS
    ):
        return fn.name
    return None


def _is_suspension_call(
    node,
    module_aliases: dict[str, str],
    value_aliases: dict[str, str],
) -> bool:
    return _suspension_call_export(node, module_aliases, value_aliases) is not None


def vthread_proven_suspension_call_key(
    module: Module,
    fd: FuncDef,
    node,
    proof_cache: Optional[dict] = None,
) -> Optional[str]:
    """Return the canonical primitive only for a lexically proven call."""
    binding_state = None
    binding_key = ("function-bindings", id(fd))
    if proof_cache is not None:
        binding_state = proof_cache.get(binding_key)
    if binding_state is None:
        aliases = None
        aliases_key = ("module-aliases", id(module))
        if proof_cache is not None:
            aliases = proof_cache.get(aliases_key)
        if aliases is None:
            aliases = _vthread_import_aliases(module)
            if proof_cache is not None:
                proof_cache[aliases_key] = aliases
        module_aliases, value_aliases = aliases
        binding_state = _function_vthread_bindings(
            fd,
            module_aliases,
            value_aliases,
        )
        if proof_cache is not None:
            proof_cache[binding_key] = binding_state
    effective_modules, effective_values, _blocked, _um, _uv = binding_state
    export = _suspension_call_export(
        node,
        effective_modules,
        effective_values,
    )
    if export is None:
        return None
    return "pcc.virtual_thread." + export


def vthread_proven_suspension_module_alias(
    module: Module,
    fd: FuncDef,
    ident: str,
) -> bool:
    module_aliases, value_aliases = _vthread_import_aliases(module)
    effective_modules, _values, _blocked, _um, _uv = (
        _function_vthread_bindings(fd, module_aliases, value_aliases)
    )
    return effective_modules.get(ident) == "pcc.virtual_thread"


def vthread_proven_value_alias(
    module: Module,
    fd: FuncDef,
    ident: str,
    export_name: str,
) -> bool:
    """Prove one callable vthread from-import without implying may_park."""

    module_aliases, value_aliases = _vthread_import_aliases(module)
    _modules, effective_values, _blocked, _um, _uv = (
        _function_vthread_bindings(fd, module_aliases, value_aliases)
    )
    return effective_values.get(ident) == export_name


def vthread_proven_suspension_value_alias(
    module: Module,
    fd: FuncDef,
    ident: str,
    export_name: str,
) -> bool:
    """Prove a vthread value alias only when it is an effect root."""

    return export_name in _SUSPENSION_EXPORTS and vthread_proven_value_alias(
        module,
        fd,
        ident,
        export_name,
    )


def vthread_proven_direct_name_call(
    fd: FuncDef,
    node,
    candidate_names: set[str],
    proof_cache: Optional[dict] = None,
) -> Optional[str]:
    """Resolve a module/sibling call only when no function local shadows it."""
    if not isinstance(node, Call) or not isinstance(node.func, Name):
        return None
    name = node.func.ident
    if name not in candidate_names:
        return None
    binding_key = ("direct-bindings", id(fd))
    binding_state = None
    if proof_cache is not None:
        binding_state = proof_cache.get(binding_key)
    if binding_state is None:
        binding_state = _function_vthread_bindings(fd, {}, {})
        if proof_cache is not None:
            proof_cache[binding_key] = binding_state
    _modules, _values, blocked_names, _um, _uv = binding_state
    if name in blocked_names:
        return None
    return name


def _scan_function_effects(
    fd: FuncDef,
    function_names: set[str],
    module_aliases: dict[str, str],
    value_aliases: dict[str, str],
    imported_functions: Optional[dict[str, tuple[str, str]]] = None,
    imported_modules: Optional[dict[str, str]] = None,
    native_exports: Optional[dict] = None,
    *,
    scope_nodes: Optional[list[object]] = None,
    binding_state=None,
) -> tuple[bool, list[str], list[tuple[str, str]]]:
    direct_suspend = False
    callees: list[str] = []
    sibling_callees: list[tuple[str, str]] = []
    if binding_state is None:
        binding_state = _function_vthread_bindings(
            fd,
            module_aliases,
            value_aliases,
            scope_nodes,
        )
    (
        effective_modules,
        effective_values,
        blocked_names,
        _uncertain_modules,
        _uncertain_values,
    ) = binding_state
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    for node in nodes:
        if isinstance(node, (FuncDef, ClassDef, Lambda)):
            continue
        if isinstance(node, Call):
            if _is_suspension_call(node, effective_modules, effective_values):
                direct_suspend = True
            elif isinstance(node.func, Name):
                callee = node.func.ident
                if callee in blocked_names:
                    pass
                elif callee in function_names and callee not in callees:
                    callees.append(callee)
                elif imported_functions is not None:
                    target = imported_functions.get(callee)
                    if target is not None and target not in sibling_callees:
                        sibling_callees.append(target)
            elif (
                isinstance(node.func, Attr)
                and isinstance(node.func.obj, Name)
                and imported_modules is not None
            ):
                module_name = node.func.obj.ident
                sibling_module = None
                if module_name not in blocked_names:
                    sibling_module = imported_modules.get(module_name)
                if sibling_module is not None:
                    target = _export_function_target(
                        native_exports,
                        sibling_module,
                        node.func.name,
                    )
                    if target is not None and target not in sibling_callees:
                        sibling_callees.append(target)
    return direct_suspend, callees, sibling_callees


def compute_vthread_may_park_functions(
    module: Module,
    native_exports: Optional[dict] = None,
) -> tuple[set[int], set[str]]:
    """Compute this module's transitive ``may_park`` fixed point.

    The id set keeps declaration decisions attached to exact ``FuncDef``
    nodes; the name set is the direct-call/spawn lookup used by lowering.
    Duplicate module-level definitions are conservatively joined by name,
    matching Python's runtime rebinding boundary rather than selecting one
    definition nondeterministically.
    """
    function_ids, function_names, _method_ids, _method_keys = (
        _compute_local_vthread_effects(module, native_exports)
    )
    return function_ids, function_names


def compute_vthread_may_park_callables(
    module: Module,
    native_exports: Optional[dict] = None,
) -> tuple[set[int], set[str], set[int], set[str]]:
    """Compute function and method effects in one shared module pass."""

    return _compute_local_vthread_effects(module, native_exports)


def _class_method_defs(module: Module) -> list[tuple[str, FuncDef]]:
    methods: list[tuple[str, FuncDef]] = []
    for stmt in module.body:
        if not isinstance(stmt, ClassDef):
            continue
        for body_stmt in stmt.body:
            if isinstance(body_stmt, FuncDef):
                methods.append((stmt.name, body_stmt))
    return methods


def _class_method_tables(
    module: Module,
) -> tuple[dict[str, dict[str, FuncDef]], dict[str, tuple[str, ...]]]:
    methods: dict[str, dict[str, FuncDef]] = {}
    bases: dict[str, tuple[str, ...]] = {}
    for stmt in module.body:
        if not isinstance(stmt, ClassDef):
            continue
        table: dict[str, FuncDef] = {}
        for body_stmt in stmt.body:
            if isinstance(body_stmt, FuncDef):
                table[body_stmt.name] = body_stmt
        methods[stmt.name] = table
        base_names: list[str] = []
        for base in stmt.bases:
            if isinstance(base, Name):
                base_names.append(base.ident)
        bases[stmt.name] = tuple(base_names)
    return methods, bases


def _receiver_class_name(
    receiver,
    current_class: Optional[str],
    local_classes: set[str],
    local_hints: Optional[dict[str, str]] = None,
) -> Optional[str]:
    if isinstance(receiver, Name):
        if receiver.ident in ("self", "cls") and current_class is not None:
            return current_class
        if receiver.ident in local_classes:
            return receiver.ident
        if local_hints is not None:
            hinted = local_hints.get(receiver.ident)
            if hinted in local_classes:
                return hinted
    receiver_ty = getattr(receiver, "ty", None)
    if isinstance(receiver_ty, ClassType):
        candidate = receiver_ty.name
        if candidate in local_classes:
            return candidate
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[1]
            if candidate in local_classes:
                return candidate
    if isinstance(receiver, Call) and isinstance(receiver.func, Name):
        if receiver.func.ident in local_classes:
            return receiver.func.ident
    return None


def _annotation_local_class_name(annotation, local_classes: set[str]) -> str:
    if isinstance(annotation, ClassType):
        candidate = annotation.name
        if candidate in local_classes:
            return candidate
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[1]
            if candidate in local_classes:
                return candidate
    return ""


def _function_local_class_hints(
    fd: Optional[FuncDef],
    local_classes: set[str],
    scope_nodes: Optional[list[object]] = None,
) -> dict[str, str]:
    hints: dict[str, str] = {}
    if fd is None:
        return hints
    for arg in fd.args:
        candidate = _annotation_local_class_name(arg.annotation, local_classes)
        if candidate:
            hints[arg.name] = candidate
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    for node in nodes:
        if isinstance(node, (FuncDef, ClassDef)):
            continue
        # Avoid importing assignment node classes into this analysis: the
        # typed AST exposes the stable targets/value fields used here.
        targets = getattr(node, "targets", ())
        value = getattr(node, "value", None)
        if (
            isinstance(targets, tuple)
            and len(targets) == 1
            and isinstance(targets[0], Name)
            and isinstance(value, Call)
            and isinstance(value.func, Name)
            and value.func.ident in local_classes
        ):
            hints[targets[0].ident] = value.func.ident
    return hints


def _resolve_local_method_key(
    class_name: str,
    method_name: str,
    methods: dict[str, dict[str, FuncDef]],
    bases: dict[str, tuple[str, ...]],
) -> Optional[str]:
    pending = [class_name]
    seen: set[str] = set()
    while pending:
        owner = pending.pop(0)
        if owner in seen:
            continue
        seen.add(owner)
        if method_name in methods.get(owner, {}):
            return owner + "." + method_name
        for base in bases.get(owner, ()):
            if base in methods:
                pending.append(base)
    return None


def vthread_proven_method_call_key(
    expr: Call,
    current_class: Optional[str],
    method_keys: Optional[set[str]],
    module: Module,
    fd: Optional[FuncDef] = None,
    *,
    methods: Optional[dict[str, dict[str, FuncDef]]] = None,
    bases: Optional[dict[str, tuple[str, ...]]] = None,
    local_hints: Optional[dict[str, str]] = None,
    proof_cache: Optional[dict] = None,
) -> Optional[str]:
    """Resolve a concrete local method, optionally restricted to effect keys."""
    if not isinstance(expr.func, Attr):
        return None
    if methods is None or bases is None:
        tables_key = ("class-tables", id(module))
        tables = None
        if proof_cache is not None:
            tables = proof_cache.get(tables_key)
        if tables is None:
            tables = _class_method_tables(module)
            if proof_cache is not None:
                proof_cache[tables_key] = tables
        methods, bases = tables
    local_classes = set(methods)
    if local_hints is None:
        hints_key = ("local-class-hints", id(fd) if fd is not None else 0)
        if proof_cache is not None:
            local_hints = proof_cache.get(hints_key)
        if local_hints is None:
            local_hints = _function_local_class_hints(fd, local_classes)
            if proof_cache is not None:
                proof_cache[hints_key] = local_hints
    class_name = _receiver_class_name(
        expr.func.obj,
        current_class,
        local_classes,
        local_hints,
    )
    if class_name is None:
        return None
    key = _resolve_local_method_key(
        class_name,
        expr.func.name,
        methods,
        bases,
    )
    if key is None or (method_keys is not None and key not in method_keys):
        return None
    return key


def _vthread_proven_export_method_call_target(
    expr: Call,
    native_exports: Optional[dict],
    module: Optional[Module] = None,
    proof_cache: Optional[dict] = None,
) -> Optional[tuple[str, str, str, dict]]:
    """Resolve one compiled-sibling method without reading its effect bit."""
    if native_exports is None or not isinstance(expr.func, Attr):
        return None
    receiver = expr.func.obj
    receiver_ty = getattr(receiver, "ty", None)
    module_name = ""
    class_name = ""
    if isinstance(receiver_ty, ClassType):
        module_name = receiver_ty.module
        class_name = receiver_ty.name
        if not module_name and "." in class_name:
            module_name, class_name = class_name.rsplit(".", 1)
    if not module_name and module is not None:
        bindings_key = ("export-class-bindings", id(module))
        bindings = None
        if proof_cache is not None:
            bindings = proof_cache.get(bindings_key)
        if bindings is None:
            bindings = _cross_module_class_bindings(module, native_exports)
            if proof_cache is not None:
                proof_cache[bindings_key] = bindings
        binding_name = ""
        if isinstance(receiver, Name):
            binding_name = receiver.ident
        elif isinstance(receiver, Call) and isinstance(receiver.func, Name):
            binding_name = receiver.func.ident
        target = bindings.get(binding_name)
        if target is not None:
            module_name, class_name = target
        if not module_name and isinstance(receiver, Name):
            # A typed local assigned from an imported constructor may retain
            # only the class leaf in early analysis.  Resolve that leaf through
            # the frozen import table; ambiguous leaves remain unresolved.
            leaf_name = getattr(receiver_ty, "name", "")
            leaf_target = bindings.get(leaf_name)
            if leaf_target is not None:
                module_name, class_name = leaf_target
    if not module_name:
        return None
    class_info = native_exports.get(module_name, {}).get(class_name)
    if not isinstance(class_info, dict) or class_info.get("kind") != "class":
        return None
    for method_info in class_info.get("methods", ()):
        if not isinstance(method_info, dict):
            continue
        if method_info.get("name") != expr.func.name:
            continue
        owning_module = class_info.get("owning_module")
        if not isinstance(owning_module, str) or not owning_module:
            owning_module = module_name
        export_name = class_info.get("export_name")
        if not isinstance(export_name, str) or not export_name:
            export_name = class_name
        return owning_module, export_name, class_name, method_info
    return None


def vthread_proven_export_method_call_key(
    expr: Call,
    native_exports: Optional[dict],
    module: Optional[Module] = None,
    proof_cache: Optional[dict] = None,
) -> Optional[str]:
    """Resolve one statically typed compiled-sibling method effect."""
    target = _vthread_proven_export_method_call_target(
        expr,
        native_exports,
        module,
        proof_cache,
    )
    if target is None or not bool(target[3].get("may_park", False)):
        return None
    return target[0] + "." + target[2] + "." + expr.func.name


def _local_method_call_keys(
    fd: FuncDef,
    current_class: Optional[str],
    methods: dict[str, dict[str, FuncDef]],
    bases: dict[str, tuple[str, ...]],
    *,
    attribute_calls: Optional[list[Call]] = None,
    local_hints: Optional[dict[str, str]] = None,
) -> list[str]:
    keys: list[str] = []
    local_classes = set(methods)
    if local_hints is None:
        local_hints = _function_local_class_hints(fd, local_classes)
    calls = attribute_calls
    if calls is None:
        calls = _function_attr_calls(fd)
    for call in calls:
        class_name = _receiver_class_name(
                call.func.obj,
                current_class,
                local_classes,
                local_hints,
            )
        if class_name is not None:
            key = _resolve_local_method_key(
                class_name,
                call.func.name,
                methods,
                bases,
            )
            if key is not None and key not in keys:
                keys.append(key)
    return keys


_BUILTIN_VALUE_RECEIVER_TYPES = (
    StrType,
    BytesType,
    ListType,
    SetType,
    DictType,
    TupleType,
    IntType,
    FloatType,
    BoolType,
    NoneType,
)


_VTHREAD_EFFECT_SUMMARY_SCHEMA = "pcc.vthread.effect-summary.v1"


def _vthread_effect_function_key(
    module_name: str,
    function_name: str,
    native_exports: dict,
) -> str:
    info = native_exports.get(module_name, {}).get(function_name)
    if isinstance(info, dict) and info.get("kind") == "function":
        module_name = str(info.get("owning_module", module_name))
        function_name = str(info.get("export_name", function_name))
    return "f:" + module_name + ":" + function_name


def _vthread_effect_method_key(
    module_name: str,
    class_name: str,
    method_name: str,
    native_exports: dict,
) -> str:
    info = native_exports.get(module_name, {}).get(class_name)
    if isinstance(info, dict) and info.get("kind") == "class":
        module_name = str(info.get("owning_module", module_name))
        class_name = str(info.get("export_name", class_name))
    return "m:" + module_name + ":" + class_name + ":" + method_name


def _vthread_summary_append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _vthread_summary_add_edge(summary: dict, caller: str, callee: str) -> None:
    edges = summary["edges"]
    index = 0
    while index + 1 < len(edges):
        if edges[index] == caller and edges[index + 1] == callee:
            return
        index += 2
    edges.append(caller)
    edges.append(callee)


def _vthread_summary_reset_caller(summary: dict, caller: str) -> None:
    seeds = summary["seeds"]
    while caller in seeds:
        seeds.remove(caller)
    edges = summary["edges"]
    kept = []
    index = 0
    while index + 1 < len(edges):
        if edges[index] != caller:
            kept.append(edges[index])
            kept.append(edges[index + 1])
        index += 2
    summary["edges"] = kept


def _add_local_callable_effect(
    node_key: str,
    fd: FuncDef,
    current_class: Optional[str],
    function_names: set[str],
    module_aliases: dict[str, str],
    value_aliases: dict[str, str],
    imported_functions: dict[str, tuple[str, str]],
    imported_modules: dict[str, str],
    native_exports: Optional[dict],
    methods: dict[str, dict[str, FuncDef]],
    bases: dict[str, tuple[str, ...]],
    module: Module,
    graph: dict[str, list[str]],
    effects: set[str],
    threading_context,
    callable_context,
    proof_cache,
    effect_summary: Optional[dict] = None,
    summary_module_name: str = "",
) -> None:
    """Add one function/method node without a self-host closure."""
    scope_nodes, binding_state, attribute_calls, local_class_hints = (
        callable_context[id(fd)]
    )
    direct, callees, siblings = _scan_function_effects(
        fd,
        function_names,
        module_aliases,
        value_aliases,
        imported_functions,
        imported_modules,
        native_exports,
        scope_nodes=scope_nodes,
        binding_state=binding_state,
    )
    edges: list[str] = []
    for callee in callees:
        edge = "function:" + callee
        if edge not in edges:
            edges.append(edge)
    method_callees = _local_method_call_keys(
        fd,
        current_class,
        methods,
        bases,
        attribute_calls=attribute_calls,
        local_hints=local_class_hints,
    )
    for method_key in method_callees:
        edge = "method:" + method_key
        if edge not in edges:
            edges.append(edge)
    graph[node_key] = edges
    direct_effect = direct or _function_has_threading_suspension(
        fd,
        module,
        current_class,
        threading_context=threading_context,
        attribute_calls=attribute_calls,
    )
    summary_caller = ""
    if effect_summary is not None and native_exports is not None:
        if current_class is None:
            summary_caller = _vthread_effect_function_key(
                summary_module_name,
                fd.name,
                native_exports,
            )
        else:
            summary_caller = _vthread_effect_method_key(
                summary_module_name,
                current_class,
                fd.name,
                native_exports,
            )
        _vthread_summary_reset_caller(effect_summary, summary_caller)
        _vthread_summary_append_unique(effect_summary["publish"], summary_caller)
        for callee in callees:
            _vthread_summary_add_edge(
                effect_summary,
                summary_caller,
                _vthread_effect_function_key(
                    summary_module_name,
                    callee,
                    native_exports,
                ),
            )
        for method_key in method_callees:
            class_name, method_name = method_key.split(".", 1)
            _vthread_summary_add_edge(
                effect_summary,
                summary_caller,
                _vthread_effect_method_key(
                    summary_module_name,
                    class_name,
                    method_name,
                    native_exports,
                ),
            )
    if direct_effect:
        effects.add(node_key)
        if effect_summary is not None and summary_caller:
            _vthread_summary_append_unique(
                effect_summary["seeds"],
                summary_caller,
            )
        return
    for call in attribute_calls:
        target = _vthread_proven_export_method_call_target(
            call,
            native_exports,
            module,
            proof_cache,
        )
        if target is None:
            continue
        if effect_summary is not None and summary_caller:
            _vthread_summary_add_edge(
                effect_summary,
                summary_caller,
                _vthread_effect_method_key(
                    target[0],
                    target[1],
                    str(target[3].get("name", "")),
                    native_exports,
                ),
            )
        if bool(target[3].get("may_park", False)):
            effects.add(node_key)
            return
    for target in siblings:
        if effect_summary is not None and summary_caller:
            _vthread_summary_add_edge(
                effect_summary,
                summary_caller,
                _vthread_effect_function_key(
                    target[0],
                    target[1],
                    native_exports,
                ),
            )
        if _export_target_is_may_park(native_exports, target):
            effects.add(node_key)
            return


def _compute_local_vthread_effects(
    module: Module,
    native_exports: Optional[dict],
    *,
    effect_summary: Optional[dict] = None,
    summary_module_name: str = "",
) -> tuple[set[int], set[str], set[int], set[str]]:
    """Compute one fixed point spanning functions and proven local methods."""
    module_aliases, value_aliases = _vthread_import_aliases(module)
    imported_functions, imported_modules = _cross_module_bindings(
        module,
        native_exports,
    )
    functions = _module_function_defs(module)
    function_names = {fd.name for fd in functions}
    for local_name in tuple(imported_functions):
        if local_name in function_names:
            imported_functions.pop(local_name, None)

    methods, bases = _class_method_tables(module)
    method_defs = _class_method_defs(module)
    local_classes = set(methods)
    callable_context = {}
    for fd in functions:
        nodes = _function_scope_nodes(fd)
        callable_context[id(fd)] = (
            nodes,
            _function_vthread_bindings(
                fd,
                module_aliases,
                value_aliases,
                nodes,
            ),
            _function_attr_calls(fd, nodes),
            _function_local_class_hints(fd, local_classes, nodes),
        )
    for _class_name, fd in method_defs:
        nodes = _function_scope_nodes(fd)
        callable_context[id(fd)] = (
            nodes,
            _function_vthread_bindings(
                fd,
                module_aliases,
                value_aliases,
                nodes,
            ),
            _function_attr_calls(fd, nodes),
            _function_local_class_hints(fd, local_classes, nodes),
        )
    threading_context = _threading_analysis_context(
        module,
        functions,
        method_defs,
        callable_context,
    )
    proof_cache = {}
    graph: dict[str, list[str]] = {}
    effects: set[str] = set()

    for fd in functions:
        _add_local_callable_effect(
            "function:" + fd.name,
            fd,
            None,
            function_names,
            module_aliases,
            value_aliases,
            imported_functions,
            imported_modules,
            native_exports,
            methods,
            bases,
            module,
            graph,
            effects,
            threading_context,
            callable_context,
            proof_cache,
            effect_summary,
            summary_module_name,
        )
    for class_name, fd in method_defs:
        _add_local_callable_effect(
            "method:" + class_name + "." + fd.name,
            fd,
            class_name,
            function_names,
            module_aliases,
            value_aliases,
            imported_functions,
            imported_modules,
            native_exports,
            methods,
            bases,
            module,
            graph,
            effects,
            threading_context,
            callable_context,
            proof_cache,
            effect_summary,
            summary_module_name,
        )

    changed = True
    while changed:
        changed = False
        for caller, callees in graph.items():
            if caller in effects:
                continue
            for callee in callees:
                if callee in effects:
                    effects.add(caller)
                    changed = True
                    break

    function_effect_names: set[str] = set()
    function_effect_ids: set[int] = set()
    for fd in functions:
        if "function:" + fd.name in effects:
            function_effect_names.add(fd.name)
            function_effect_ids.add(id(fd))
    for local_name, target in imported_functions.items():
        if _export_target_is_may_park(native_exports, target):
            function_effect_names.add(local_name)

    method_effect_keys: set[str] = set()
    method_effect_ids: set[int] = set()
    for class_name, fd in method_defs:
        key = class_name + "." + fd.name
        if "method:" + key in effects:
            method_effect_keys.add(key)
            method_effect_ids.add(id(fd))
    return (
        function_effect_ids,
        function_effect_names,
        method_effect_ids,
        method_effect_keys,
    )


def build_closed_world_vthread_effect_summary(
    module: Module,
    module_name: str,
    native_exports: dict,
) -> dict:
    """Build one deterministic, object-free-at-consumption module summary."""
    summary = {
        "schema": _VTHREAD_EFFECT_SUMMARY_SCHEMA,
        "module_name": module_name,
        "seeds": [],
        "edges": [],
        "publish": [],
    }
    _compute_local_vthread_effects(
        module,
        native_exports,
        effect_summary=summary,
        summary_module_name=module_name,
    )
    return summary


def closed_world_vthread_effect_export_surface(native_exports: dict) -> dict:
    """Project only metadata read by per-module vthread summary workers."""
    result = {}
    for module_name, exports in native_exports.items():
        module_result = {}
        for export_name, info in exports.items():
            if not isinstance(info, dict):
                continue
            kind = info.get("kind")
            if kind == "function":
                module_result[export_name] = {
                    "kind": "function",
                    "owning_module": str(
                        info.get("owning_module", module_name)
                    ),
                    "export_name": str(info.get("export_name", export_name)),
                    "semantic_decorator": bool(
                        info.get("semantic_decorator", False)
                    ),
                    "may_park": bool(info.get("may_park", False)),
                }
                continue
            if kind != "class":
                continue
            methods = []
            for method_info in info.get("methods", ()):
                if isinstance(method_info, dict):
                    methods.append(
                        {
                            "name": str(method_info.get("name", "")),
                            "may_park": bool(method_info.get("may_park", False)),
                        }
                    )
            module_result[export_name] = {
                "kind": "class",
                "owning_module": str(info.get("owning_module", module_name)),
                "export_name": str(info.get("export_name", export_name)),
                "methods": tuple(methods),
            }
        result[module_name] = module_result
    return result


def write_closed_world_vthread_effect_summary(path: str, summary: dict) -> None:
    if summary.get("schema") != _VTHREAD_EFFECT_SUMMARY_SCHEMA:
        raise ValueError("invalid vthread effect summary schema")
    _write_vthread_effect_summary_wire(
        path,
        summary["module_name"],
        summary["seeds"],
        summary["edges"],
        summary["publish"],
    )


def read_closed_world_vthread_effect_summary(path: str) -> dict:
    payload = _read_vthread_effect_summary_wire(path)
    payload["schema"] = _VTHREAD_EFFECT_SUMMARY_SCHEMA
    return payload


def _vthread_effect_node_id(
    node_ids: dict[str, int],
    effects: CompilerIntArena,
    key: str,
) -> int:
    node_id = node_ids.get(key, -1)
    if node_id < 0:
        node_id = len(effects)
        node_ids[key] = node_id
        effects.append(0)
    return node_id


def annotate_closed_world_vthread_effect_summaries(
    summary_paths: list[str],
    native_exports: dict,
) -> tuple[int, int, int]:
    """Merge module wires and solve their dense-ID fixed point in the parent."""
    node_ids: dict[str, int] = {}
    publish_keys: dict[str, bool] = {}
    effects = CompilerIntArena()
    edge_pairs = CompilerIntArena()
    try:
        for module_name, exports in native_exports.items():
            for export_name, info in exports.items():
                if not isinstance(info, dict):
                    continue
                if info.get("kind") == "function":
                    if bool(info.get("may_park", False)):
                        key = _vthread_effect_function_key(
                            module_name, export_name, native_exports
                        )
                        node_id = _vthread_effect_node_id(node_ids, effects, key)
                        effects.set_unchecked(node_id, 1)
                    continue
                if info.get("kind") != "class":
                    continue
                for method_info in info.get("methods", ()):
                    if not isinstance(method_info, dict) or not bool(
                        method_info.get("may_park", False)
                    ):
                        continue
                    key = _vthread_effect_method_key(
                        module_name,
                        export_name,
                        str(method_info.get("name", "")),
                        native_exports,
                    )
                    node_id = _vthread_effect_node_id(node_ids, effects, key)
                    effects.set_unchecked(node_id, 1)

        for path in summary_paths:
            summary = read_closed_world_vthread_effect_summary(path)
            for key in summary["seeds"]:
                node_id = _vthread_effect_node_id(node_ids, effects, key)
                effects.set_unchecked(node_id, 1)
            for key in summary["publish"]:
                publish_keys[key] = True
                _vthread_effect_node_id(node_ids, effects, key)
            edges = summary["edges"]
            index = 0
            while index < len(edges):
                edge_pairs.append2(
                    _vthread_effect_node_id(node_ids, effects, edges[index]),
                    _vthread_effect_node_id(
                        node_ids, effects, edges[index + 1]
                    ),
                )
                index += 2

        changed = True
        while changed:
            changed = False
            index = 0
            while index < len(edge_pairs):
                caller = edge_pairs.get_unchecked(index)
                callee = edge_pairs.get_unchecked(index + 1)
                if effects.get_unchecked(callee) and not effects.get_unchecked(
                    caller
                ):
                    effects.set_unchecked(caller, 1)
                    changed = True
                index += 2

        for module_name, exports in native_exports.items():
            for export_name, info in exports.items():
                if not isinstance(info, dict):
                    continue
                if info.get("kind") == "function":
                    key = _vthread_effect_function_key(
                        module_name, export_name, native_exports
                    )
                    if publish_keys.get(key, False):
                        info["may_park"] = bool(
                            effects.get_unchecked(node_ids[key])
                        )
                    continue
                if info.get("kind") != "class":
                    continue
                for method_info in info.get("methods", ()):
                    if not isinstance(method_info, dict):
                        continue
                    key = _vthread_effect_method_key(
                        module_name,
                        export_name,
                        str(method_info.get("name", "")),
                        native_exports,
                    )
                    if publish_keys.get(key, False):
                        method_info["may_park"] = bool(
                            effects.get_unchecked(node_ids[key])
                        )
        return len(summary_paths), len(effects), len(edge_pairs) // 2
    finally:
        effects.close()
        edge_pairs.close()


def compute_vthread_may_park_methods(
    module: Module,
    may_park_function_names: Optional[set[str]] = None,
    native_exports: Optional[dict] = None,
) -> tuple[set[int], set[str]]:
    """Return concrete local class methods lowered to continuation ABI."""
    _function_ids, _function_names, method_ids, method_keys = (
        _compute_local_vthread_effects(module, native_exports)
    )
    return method_ids, method_keys


def _collect_all_method_effect_names(
    module: Module,
    native_exports: Optional[dict],
    may_park_method_keys: set[str],
) -> set[str]:
    names: set[str] = set()
    for class_name, fd in _class_method_defs(module):
        if class_name + "." + fd.name in may_park_method_keys:
            names.add(fd.name)
    if native_exports is None:
        return names
    for exports in native_exports.values():
        if not isinstance(exports, dict):
            continue
        for class_info in exports.values():
            if not isinstance(class_info, dict):
                continue
            if class_info.get("kind") != "class":
                continue
            for method_info in class_info.get("methods", ()):
                if (
                    isinstance(method_info, dict)
                    and bool(method_info.get("may_park", False))
                ):
                    names.add(str(method_info.get("name", "")))
    return names


def _unresolved_method_effect_reason(
    fd: FuncDef,
    current_class: Optional[str],
    module: Module,
    native_exports: Optional[dict],
    may_park_method_keys: set[str],
    effect_method_names: set[str],
    *,
    vthread_modules: Optional[dict[str, str]] = None,
    vthread_values: Optional[dict[str, str]] = None,
    attribute_calls: Optional[list[Call]] = None,
    binding_state=None,
    methods: Optional[dict[str, dict[str, FuncDef]]] = None,
    bases: Optional[dict[str, tuple[str, ...]]] = None,
    local_class_hints: Optional[dict[str, str]] = None,
    proof_cache: Optional[dict] = None,
) -> str:
    if vthread_modules is None or vthread_values is None:
        vthread_modules, vthread_values = _vthread_import_aliases(module)
    if binding_state is None:
        binding_state = _function_vthread_bindings(
            fd,
            vthread_modules,
            vthread_values,
        )
    (
        effective_vthread_modules,
        _effective_vthread_values,
        _blocked_vthread_names,
        _uncertain_vthread_modules,
        _uncertain_vthread_values,
    ) = binding_state
    calls = attribute_calls
    if calls is None:
        calls = _function_attr_calls(fd)
    for call in calls:
        attr_name = call.func.name
        if attr_name not in effect_method_names:
            continue
        if isinstance(getattr(call.func.obj, "ty", None), _BUILTIN_VALUE_RECEIVER_TYPES):
            # ``", ".join(...)``, ``payload.read(...)`` on a str/bytes/list/
            # dict/tuple/set/int/float/bool/None receiver is a builtin method,
            # never an open-world user method that could park.
            continue
        if (
            isinstance(call.func.obj, Name)
            and effective_vthread_modules.get(call.func.obj.ident)
            == "pcc.virtual_thread"
            and attr_name in _VTHREAD_VALUE_EXPORTS
        ):
            # A proven, unshadowed pcc.virtual_thread export is a native
            # primitive, not an open-world user method which happens to share
            # a name such as ``join`` or ``cancel``.
            continue
        if (
            vthread_proven_method_call_key(
                call,
                current_class,
                None,
                module,
                fd,
                methods=methods,
                bases=bases,
                local_hints=local_class_hints,
                proof_cache=proof_cache,
            )
            is not None
        ):
            continue
        if (
            _vthread_proven_export_method_call_target(
                call,
                native_exports,
                module,
                proof_cache,
            )
            is not None
        ):
            continue
        receiver_ty = getattr(call.func.obj, "ty", None)
        return (
            "unresolved user-method may park: ." + attr_name
            + " (receiver type " + repr(getattr(receiver_ty, "name", None))
            + ", module " + repr(getattr(receiver_ty, "module", None)) + ")"
        )
    return ""


def _function_attr_calls(
    fd: FuncDef,
    scope_nodes: Optional[list[object]] = None,
) -> list[Call]:
    calls: list[Call] = []
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    for node in nodes:
        if isinstance(node, (FuncDef, ClassDef)):
            continue
        if isinstance(node, Call) and isinstance(node.func, Attr):
            calls.append(node)
    return calls


def _threading_import_bindings(
    module: Module,
) -> tuple[set[str], dict[str, str]]:
    module_aliases: set[str] = set()
    value_aliases: dict[str, str] = {}
    for stmt in _module_scope_imports(module):
        if isinstance(stmt, Import):
            for imported_module, as_name in stmt.names:
                if imported_module == "threading":
                    module_aliases.add(as_name or "threading")
        elif (
            isinstance(stmt, ImportFrom)
            and (stmt.level or 0) == 0
            and (stmt.module or "") == "threading"
        ):
            for imported_name, as_name in stmt.names:
                if imported_name in _THREADING_SUSPENSION_METHODS:
                    value_aliases[as_name or imported_name] = imported_name
    return module_aliases, value_aliases


def _threading_constructor_kind(
    expr,
    module_aliases: set[str],
    value_aliases: dict[str, str],
) -> Optional[str]:
    if not isinstance(expr, Call):
        return None
    if isinstance(expr.func, Name):
        return value_aliases.get(expr.func.ident)
    if (
        isinstance(expr.func, Attr)
        and isinstance(expr.func.obj, Name)
        and expr.func.obj.ident in module_aliases
        and expr.func.name in _THREADING_SUSPENSION_METHODS
    ):
        return expr.func.name
    return None


def _threading_assignment_hints(
    fd: FuncDef,
    module_aliases: set[str],
    value_aliases: dict[str, str],
    scope_nodes: Optional[list[object]] = None,
) -> tuple[dict[str, str], dict[str, str]]:
    locals_by_name: dict[str, str] = {}
    fields_by_name: dict[str, str] = {}
    self_name = fd.args[0].name if fd.args else "self"
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    for node in nodes:
        if not isinstance(node, Assign):
            continue
        kind = _threading_constructor_kind(
            node.value,
            module_aliases,
            value_aliases,
        )
        if kind is None:
            continue
        for target in node.targets:
            if isinstance(target, Name):
                locals_by_name[target.ident] = kind
            elif (
                isinstance(target, Attr)
                and isinstance(target.obj, Name)
                and target.obj.ident == self_name
            ):
                fields_by_name[target.name] = kind
    return locals_by_name, fields_by_name


def _threading_module_name_hints(
    module: Module,
    module_aliases: set[str],
    value_aliases: dict[str, str],
) -> dict[str, str]:
    hints: dict[str, str] = {}
    for node in module.body:
        if not isinstance(node, Assign):
            continue
        kind = _threading_constructor_kind(
            node.value,
            module_aliases,
            value_aliases,
        )
        if kind is None:
            continue
        for target in node.targets:
            if isinstance(target, Name):
                hints[target.ident] = kind
    return hints


def _threading_class_field_hints(
    module: Module,
    class_name: Optional[str],
    module_aliases: set[str],
    value_aliases: dict[str, str],
) -> dict[str, str]:
    if class_name is None:
        return {}
    hints: dict[str, str] = {}
    for stmt in module.body:
        if not isinstance(stmt, ClassDef) or stmt.name != class_name:
            continue
        for child in stmt.body:
            if not isinstance(child, FuncDef):
                continue
            _locals, fields = _threading_assignment_hints(
                child,
                module_aliases,
                value_aliases,
            )
            for name, kind in fields.items():
                prior = hints.get(name)
                if prior is None or prior == kind:
                    hints[name] = kind
                else:
                    # Conflicting constructor assignments make the field
                    # dynamic; do not guess a parking ABI.
                    hints.pop(name, None)
    return hints


def _threading_analysis_context(
    module: Module,
    functions: Optional[list[FuncDef]] = None,
    method_defs: Optional[list[tuple[str, FuncDef]]] = None,
    callable_context=None,
):
    """Precompute module-wide threading receiver evidence once.

    The old per-callable path rescanned every module import/global and every
    method in the current class for each function.  Large compiler mixins then
    paid a quadratic AST walk before emitting any IR.  Keep exactly the same
    conservative evidence, but index it by the exact ``FuncDef`` and class.
    """

    if functions is None:
        functions = _module_function_defs(module)
    if method_defs is None:
        method_defs = _class_method_defs(module)
    module_aliases, value_aliases = _threading_import_bindings(module)
    global_hints = _threading_module_name_hints(
        module,
        module_aliases,
        value_aliases,
    )
    local_hints_by_id: dict[int, dict[str, str]] = {}
    field_hints_by_class: dict[str, dict[str, str]] = {}
    field_conflicts_by_class: dict[str, set[str]] = {}

    for fd in functions:
        scope_nodes = None
        if callable_context is not None:
            scope_nodes = callable_context[id(fd)][0]
        local_hints, _fields = _threading_assignment_hints(
            fd,
            module_aliases,
            value_aliases,
            scope_nodes,
        )
        local_hints_by_id[id(fd)] = local_hints

    for class_name, fd in method_defs:
        scope_nodes = None
        if callable_context is not None:
            scope_nodes = callable_context[id(fd)][0]
        local_hints, fields = _threading_assignment_hints(
            fd,
            module_aliases,
            value_aliases,
            scope_nodes,
        )
        local_hints_by_id[id(fd)] = local_hints
        class_hints = field_hints_by_class.setdefault(class_name, {})
        conflicts = field_conflicts_by_class.setdefault(class_name, set())
        for name, kind in fields.items():
            if name in conflicts:
                continue
            prior = class_hints.get(name)
            if prior is None or prior == kind:
                class_hints[name] = kind
            else:
                class_hints.pop(name, None)
                conflicts.add(name)

    return (
        module_aliases,
        value_aliases,
        local_hints_by_id,
        field_hints_by_class,
        global_hints,
    )


def _threading_receiver_kind(
    receiver,
    module: Module,
    local_hints: dict[str, str],
    field_hints: dict[str, str],
    global_hints: dict[str, str],
    self_name: str,
) -> Optional[str]:
    """Return a proven native threading receiver leaf.

    Type inference attaches the native constructor's ``ClassType`` to local
    variables and instance fields.  A same-module class with the same leaf is
    excluded, matching native_threading's user-class guard, so a user-defined
    ``Event.wait`` cannot accidentally turn into a scheduler effect.
    """
    receiver_ty = getattr(receiver, "ty", None)
    if isinstance(receiver_ty, ClassType):
        candidate = receiver_ty.name
        if "." in candidate:
            candidate = candidate.rsplit(".", 1)[1]
        if candidate in _THREADING_SUSPENSION_METHODS:
            for stmt in module.body:
                if isinstance(stmt, ClassDef) and stmt.name == candidate:
                    return None
            return candidate
    if isinstance(receiver, Name):
        return local_hints.get(receiver.ident) or global_hints.get(
            receiver.ident
        )
    if (
        isinstance(receiver, Attr)
        and isinstance(receiver.obj, Name)
        and receiver.obj.ident == self_name
    ):
        return field_hints.get(receiver.name)
    return None


def _is_threading_suspension_call(
    node,
    module: Module,
    local_hints: dict[str, str],
    field_hints: dict[str, str],
    global_hints: dict[str, str],
    self_name: str,
) -> bool:
    if not isinstance(node, Call) or not isinstance(node.func, Attr):
        return False
    kind = _threading_receiver_kind(
        node.func.obj,
        module,
        local_hints,
        field_hints,
        global_hints,
        self_name,
    )
    if kind is None:
        return False
    return node.func.name in _THREADING_SUSPENSION_METHODS[kind]


def _function_has_threading_suspension(
    fd: FuncDef,
    module: Module,
    current_class: Optional[str] = None,
    *,
    threading_context=None,
    attribute_calls: Optional[list[Call]] = None,
) -> bool:
    if threading_context is None:
        threading_context = _threading_analysis_context(module)
    (
        _module_aliases,
        _value_aliases,
        local_hints_by_id,
        field_hints_by_class,
        global_hints,
    ) = threading_context
    local_hints = local_hints_by_id.get(id(fd), {})
    field_hints = field_hints_by_class.get(current_class or "", {})
    self_name = fd.args[0].name if current_class is not None and fd.args else "self"
    calls = attribute_calls
    if calls is None:
        calls = _function_attr_calls(fd)
    for call in calls:
        if _is_threading_suspension_call(
            call,
            module,
            local_hints,
            field_hints,
            global_hints,
            self_name,
        ):
            return True
    return False


def _function_attr_call_names(fd: FuncDef) -> list[str]:
    names: list[str] = []
    for call in _function_attr_calls(fd):
        if call.func.name not in names:
            names.append(call.func.name)
    return names


def _method_effect_requires_implicit_dispatch(fd: FuncDef) -> bool:
    if fd.name.startswith("__") and fd.name.endswith("__"):
        return True
    for decorator in fd.decorators:
        if isinstance(decorator, Name):
            if decorator.ident in ("property", "cached_property"):
                return True
        elif isinstance(decorator, Attr):
            if decorator.name in ("property", "cached_property", "setter", "deleter"):
                return True
    return False


def _shadowed_vthread_effect_reason(
    fd: FuncDef,
    module: Module,
    may_park_function_names: set[str],
    *,
    module_aliases: Optional[dict[str, str]] = None,
    value_aliases: Optional[dict[str, str]] = None,
    scope_nodes: Optional[list[object]] = None,
    binding_state=None,
) -> str:
    """Describe a call whose spelling is effectful but binding is dynamic."""
    if module_aliases is None or value_aliases is None:
        module_aliases, value_aliases = _vthread_import_aliases(module)
    if binding_state is None:
        binding_state = _function_vthread_bindings(
            fd,
            module_aliases,
            value_aliases,
            scope_nodes,
        )
    (
        effective_modules,
        effective_values,
        blocked_names,
        uncertain_modules,
        uncertain_values,
    ) = binding_state
    nodes = scope_nodes
    if nodes is None:
        nodes = _function_scope_nodes(fd)
    for node in nodes:
        if not isinstance(node, Call):
            continue
        if isinstance(node.func, Name):
            name = node.func.ident
            if name in uncertain_values:
                return "dynamic binding shadows virtual-thread primitive: " + name
            if (
                name in blocked_names
                and name in may_park_function_names
                and name not in effective_values
            ):
                return "dynamic binding shadows may_park function: " + name
            continue
        if not isinstance(node.func, Attr) or not isinstance(node.func.obj, Name):
            continue
        root = node.func.obj.ident
        if (
            root in uncertain_modules
            and root not in effective_modules
            and node.func.name in _SUSPENSION_EXPORTS
        ):
            return (
                "dynamic binding shadows virtual-thread module alias: "
                + root
                + "."
                + node.func.name
            )
    return ""


def vthread_method_owner_for_funcdef(
    module: Module,
    fd: FuncDef,
    proof_cache: Optional[dict] = None,
) -> Optional[str]:
    """Return the lexical local-class owner for one exact method node."""
    owners_key = ("method-owners", id(module))
    owners = None
    if proof_cache is not None:
        owners = proof_cache.get(owners_key)
    if owners is None:
        owners = {}
        for class_name, method_fd in _class_method_defs(module):
            owners[id(method_fd)] = class_name
        if proof_cache is not None:
            proof_cache[owners_key] = owners
    return owners.get(id(fd))


def classify_vthread_park_boundaries(
    module: Module,
    may_park_function_names: set[str],
    native_exports: Optional[dict] = None,
    may_park_method_keys: Optional[set[str]] = None,
) -> dict[str, str]:
    """Classify unresolved calls which may cross a parking method boundary.

    Concrete local receivers are continuation-lowered.  A call which only
    matches a parking method *name* but whose receiver cannot be proven stays
    an open-world boundary and is rejected explicitly instead of silently
    blocking a carrier or discarding a child continuation.
    """
    top_functions = _module_function_defs(module)
    top_names: set[str] = set()
    for fd in top_functions:
        top_names.add(fd.name)
    vthread_modules, vthread_values = _vthread_import_aliases(module)
    imported_functions, imported_modules = _cross_module_bindings(
        module,
        native_exports,
    )

    if may_park_method_keys is None:
        _method_ids, may_park_method_keys = compute_vthread_may_park_methods(
            module,
            may_park_function_names,
            native_exports,
        )
    method_defs = _class_method_defs(module)
    methods, bases = _class_method_tables(module)
    local_classes = set(methods)
    callable_context = {}
    proof_cache = {}
    for fd in top_functions:
        nodes = _function_scope_nodes(fd)
        callable_context[id(fd)] = (
            nodes,
            _function_vthread_bindings(
                fd,
                vthread_modules,
                vthread_values,
                nodes,
            ),
            _function_attr_calls(fd, nodes),
            _function_local_class_hints(fd, local_classes, nodes),
        )
    for _class_name, fd in method_defs:
        nodes = _function_scope_nodes(fd)
        callable_context[id(fd)] = (
            nodes,
            _function_vthread_bindings(
                fd,
                vthread_modules,
                vthread_values,
                nodes,
            ),
            _function_attr_calls(fd, nodes),
            _function_local_class_hints(fd, local_classes, nodes),
        )
    effect_method_names = _collect_all_method_effect_names(
        module,
        native_exports,
        may_park_method_keys,
    )

    local_edges: dict[str, list[str]] = {}
    rejected: dict[str, str] = {}
    for fd in top_functions:
        nodes, binding_state, attribute_calls, local_class_hints = (
            callable_context[id(fd)]
        )
        _direct, callees, _siblings = _scan_function_effects(
            fd,
            top_names,
            vthread_modules,
            vthread_values,
            imported_functions,
            imported_modules,
            native_exports,
            scope_nodes=nodes,
            binding_state=binding_state,
        )
        local_edges[fd.name] = callees
        reason = _shadowed_vthread_effect_reason(
            fd,
            module,
            may_park_function_names,
            module_aliases=vthread_modules,
            value_aliases=vthread_values,
            scope_nodes=nodes,
            binding_state=binding_state,
        )
        if not reason:
            reason = _unresolved_method_effect_reason(
                fd,
                None,
                module,
                native_exports,
                may_park_method_keys,
                effect_method_names,
                vthread_modules=vthread_modules,
                vthread_values=vthread_values,
                attribute_calls=attribute_calls,
                binding_state=binding_state,
                methods=methods,
                bases=bases,
                local_class_hints=local_class_hints,
                proof_cache=proof_cache,
            )
        if reason:
            rejected[fd.name] = reason

    # A continuation-lowered method must obey the same rule internally.  A
    # dynamic receiver cannot be made safe merely because its surrounding
    # method has a concrete lexical owner; record an exact class.method key so
    # declaration can reject it before emitting a mismatched ABI.
    for class_name, fd in method_defs:
        nodes, binding_state, attribute_calls, local_class_hints = (
            callable_context[id(fd)]
        )
        reason = _shadowed_vthread_effect_reason(
            fd,
            module,
            may_park_function_names,
            module_aliases=vthread_modules,
            value_aliases=vthread_values,
            scope_nodes=nodes,
            binding_state=binding_state,
        )
        if not reason:
            reason = _unresolved_method_effect_reason(
                fd,
                class_name,
                module,
                native_exports,
                may_park_method_keys,
                effect_method_names,
                vthread_modules=vthread_modules,
                vthread_values=vthread_values,
                attribute_calls=attribute_calls,
                binding_state=binding_state,
                methods=methods,
                bases=bases,
                local_class_hints=local_class_hints,
                proof_cache=proof_cache,
            )
        if reason:
            rejected[class_name + "." + fd.name] = reason
        elif (
            class_name + "." + fd.name in may_park_method_keys
            and _method_effect_requires_implicit_dispatch(fd)
        ):
            rejected[class_name + "." + fd.name] = (
                "implicit descriptor/dunder may_park dispatch is unsupported"
            )

    # Carry the reject boundary through concrete method edges first.  A
    # wrapper around an unproved dynamic receiver is not made resumable by
    # giving the wrapper itself a concrete receiver.
    rejected_method_keys: set[str] = set()
    for class_name, fd in method_defs:
        key = class_name + "." + fd.name
        if key in rejected:
            rejected_method_keys.add(key)
    changed = True
    while changed:
        changed = False
        for class_name, fd in method_defs:
            key = class_name + "." + fd.name
            if key in rejected_method_keys:
                continue
            for callee_key in _local_method_call_keys(
                fd,
                class_name,
                methods,
                bases,
                attribute_calls=callable_context[id(fd)][2],
                local_hints=callable_context[id(fd)][3],
            ):
                if callee_key in rejected_method_keys:
                    rejected_method_keys.add(key)
                    rejected[key] = (
                        "calls unresolved may_park method wrapper: "
                        + callee_key
                        + ": " + rejected[callee_key]
                    )
                    changed = True
                    break

    # Carry both direct-function and concrete-method reject boundaries through
    # top-level wrappers, matching the effect fixed point used for resumable
    # call chains.
    changed = True
    while changed:
        changed = False
        for caller, callees in local_edges.items():
            if caller in rejected:
                continue
            for callee in callees:
                if callee in rejected:
                    rejected[caller] = (
                        "calls unresolved may_park wrapper: " + callee
                        + ": " + rejected[callee]
                    )
                    changed = True
                    break
            if caller in rejected:
                continue
            caller_fd = None
            for fd in top_functions:
                if fd.name == caller:
                    caller_fd = fd
                    break
            if caller_fd is None:
                continue
            for callee_key in _local_method_call_keys(
                caller_fd,
                None,
                methods,
                bases,
                attribute_calls=callable_context[id(caller_fd)][2],
                local_hints=callable_context[id(caller_fd)][3],
            ):
                if callee_key in rejected_method_keys:
                    rejected[caller] = (
                        "calls unresolved may_park method wrapper: "
                        + callee_key
                        + ": " + rejected[callee_key]
                    )
                    changed = True
                    break
    return rejected


def annotate_closed_world_vthread_effects(
    parsed_modules: list[Module],
    module_names: list[str],
    native_exports: dict,
) -> None:
    """Publish a whole-closure ``may_park`` fixed point on function exports.

    This pass runs only after every module's initial export table exists.  It
    never invents an edge for a dynamic callable or user-method dispatch.
    """
    functions_by_module: dict[str, list[FuncDef]] = {}
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    may_park: set[tuple[str, str]] = set()

    for module, module_name in zip(parsed_modules, module_names):
        functions = _module_function_defs(module)
        threading_context = _threading_analysis_context(module, functions)
        functions_by_module[module_name] = functions
        function_names: set[str] = set()
        for fd in functions:
            function_names.add(fd.name)
        vthread_modules, vthread_values = _vthread_import_aliases(module)
        imported_functions, imported_modules = _cross_module_bindings(
            module,
            native_exports,
        )
        for local_name in tuple(imported_functions):
            if local_name in function_names:
                imported_functions.pop(local_name, None)

        for fd in functions:
            key = (module_name, fd.name)
            direct_suspend, local_callees, sibling_callees = (
                _scan_function_effects(
                    fd,
                    function_names,
                    vthread_modules,
                    vthread_values,
                    imported_functions,
                    imported_modules,
                    native_exports,
                )
            )
            edges: list[tuple[str, str]] = []
            for callee in local_callees:
                target = (module_name, callee)
                if target not in edges:
                    edges.append(target)
            for target in sibling_callees:
                if target not in edges:
                    edges.append(target)
            graph[key] = edges
            if direct_suspend or _function_has_threading_suspension(
                fd,
                module,
                threading_context=threading_context,
            ):
                may_park.add(key)

    # Preserve explicit metadata supplied by a native export provider.  This
    # also lets an independently compiled leaf seed a larger sibling closure.
    for export_module, exports in native_exports.items():
        for export_name, info in exports.items():
            if (
                isinstance(info, dict)
                and info.get("kind") == "function"
                and bool(info.get("may_park", False))
            ):
                may_park.add(
                    (
                        str(info.get("owning_module", export_module)),
                        str(info.get("export_name", export_name)),
                    )
                )

    changed = True
    while changed:
        changed = False
        for caller, callees in graph.items():
            if caller in may_park:
                continue
            for callee in callees:
                if callee in may_park:
                    may_park.add(caller)
                    changed = True
                    break

    for module_name, functions in functions_by_module.items():
        exports = native_exports.get(module_name, {})
        for fd in functions:
            info = exports.get(fd.name)
            if isinstance(info, dict) and info.get("kind") == "function":
                info["may_park"] = (module_name, fd.name) in may_park

    # Join local class-method edges into the closure metadata as well.  The
    # function-only graph above establishes sibling-function seeds; repeating
    # the local joint fixed point then lets ``handler -> Worker.run -> park``
    # publish the same generator ABI to importers.  Iterate because one
    # module's newly published function may seed an earlier sibling on the
    # next pass.
    metadata_changed = True
    while metadata_changed:
        metadata_changed = False
        for module, module_name in zip(parsed_modules, module_names):
            (
                _function_ids,
                function_effect_names,
                _method_ids,
                method_effect_keys,
            ) = _compute_local_vthread_effects(module, native_exports)
            exports = native_exports.get(module_name, {})
            for fd in _module_function_defs(module):
                info = exports.get(fd.name)
                if not isinstance(info, dict) or info.get("kind") != "function":
                    continue
                should_park = fd.name in function_effect_names
                if should_park and not bool(info.get("may_park", False)):
                    info["may_park"] = True
                    metadata_changed = True
                elif "may_park" not in info:
                    info["may_park"] = False
            for class_name, class_info in exports.items():
                if not isinstance(class_info, dict):
                    continue
                if class_info.get("kind") != "class":
                    continue
                for method_info in class_info.get("methods", ()):
                    if not isinstance(method_info, dict):
                        continue
                    method_key = str(class_name) + "." + str(
                        method_info.get("name", "")
                    )
                    should_park = method_key in method_effect_keys
                    if should_park and not bool(method_info.get("may_park", False)):
                        method_info["may_park"] = True
                        metadata_changed = True
                    elif "may_park" not in method_info:
                        method_info["may_park"] = False


def vthread_delegate_frame_name(expr: Call, callee_name: str) -> str:
    """Stable hidden frame-slot name for one transitive parking call site."""
    span = expr.span
    line = getattr(span, "line", 0) if span is not None else 0
    col = getattr(span, "col", 0) if span is not None else 0
    safe_name = callee_name.replace(".", "_").replace("-", "_")
    return (
        "__pcc_vthread_delegate_"
        + safe_name
        + "_"
        + str(line)
        + "_"
        + str(col)
    )


__all__ = [
    "annotate_closed_world_vthread_effects",
    "classify_vthread_park_boundaries",
    "compute_vthread_may_park_functions",
    "compute_vthread_may_park_callables",
    "compute_vthread_may_park_methods",
    "vthread_delegate_frame_name",
    "vthread_method_owner_for_funcdef",
    "vthread_proven_direct_name_call",
    "vthread_proven_export_method_call_key",
    "vthread_proven_method_call_key",
    "vthread_proven_suspension_call_key",
]
