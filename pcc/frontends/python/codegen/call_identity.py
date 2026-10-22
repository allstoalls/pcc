"""Semantic identities used by the method-call migration's observation path.

Spelling and annotations are not proofs. A caller supplies the possible
runtime classes and a verified lookup-stability fact. The same resolver works
for an ordinary user class and for IRBuilder. It does not emit instructions,
invent an ABI, enable a module mode, or select an external execution owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from pcc.frontends.python.py_ast import ClassDef


@dataclass(frozen=True, order=True)
class SymbolIdentity:
    module: str
    qualname: str

    def text(self) -> str:
        return self.module + ":" + self.qualname


@dataclass(frozen=True)
class ReceiverIdentity:
    """An exhaustive set of runtime classes, not a best-effort type hint.

    ``complete=False`` propagates through joins. ``lookup_stable`` also
    requires ruling out writes through aliases and unknown calls; absence of
    a subclass alone does not establish it. A nullable field can become
    non-null at a call site only with a separate control-flow proof.
    """

    classes: tuple[SymbolIdentity, ...] = ()
    complete: bool = False
    lookup_stable: bool = False
    may_be_none: bool = False
    evidence: tuple[str, ...] = ()

    def join(self, other: ReceiverIdentity) -> ReceiverIdentity:
        return ReceiverIdentity(
            classes=tuple(sorted(set(self.classes) | set(other.classes))),
            complete=self.complete and other.complete,
            lookup_stable=self.lookup_stable and other.lookup_stable,
            may_be_none=self.may_be_none or other.may_be_none,
            evidence=tuple(sorted(set(self.evidence) | set(other.evidence))),
        )


@dataclass(frozen=True)
class DeclaredCallABI:
    """A snapshot of an emitted declaration, never reconstructed annotations."""

    return_type: str
    parameter_types: tuple[str, ...]
    var_arg: bool = False


@dataclass(frozen=True)
class MethodIdentity:
    target: SymbolIdentity
    # This is the callee's declared symbol, not a symbol reconstructed from
    # the receiver's class name (inherited methods have a different owner).
    emitted_symbol: str
    kind: str = "instance"
    abi: DeclaredCallABI | None = None


@dataclass(frozen=True)
class ClassIdentity:
    symbol: SymbolIdentity
    # Supplied by the common class/MRO resolver. Do not invent a second MRO.
    lookup_order: tuple[SymbolIdentity, ...]
    methods: Mapping[str, MethodIdentity]
    instance_fields: frozenset[str] = frozenset()
    descriptors: frozenset[str] = frozenset()
    # Non-method class bindings, e.g. Child.add = None, stop the MRO search.
    class_attributes: frozenset[str] = frozenset()
    intercepted_lookup: bool = False


@dataclass(frozen=True)
class CallIdentityDecision:
    status: str
    reason: str
    target: SymbolIdentity | None = None
    emitted_symbol: str | None = None
    method_kind: str | None = None
    evidence: tuple[str, ...] = ()
    abi: DeclaredCallABI | None = None


def resolve_method_identity(
    receiver: ReceiverIdentity,
    method: str,
    classes: Mapping[SymbolIdentity, ClassIdentity],
) -> CallIdentityDecision:
    """Prove the same actual method for every admitted runtime receiver."""

    def unknown(reason: str) -> CallIdentityDecision:
        return CallIdentityDecision("unknown", reason, evidence=receiver.evidence)

    if not receiver.complete or not receiver.classes:
        return unknown("receiver classes are not exhaustive")
    if not receiver.evidence:
        return unknown("receiver proof has no provenance")
    if receiver.may_be_none:
        return unknown("receiver may be None")
    if not receiver.lookup_stable:
        return unknown("method lookup stability is not proved")
    selected: MethodIdentity | None = None
    for symbol in receiver.classes:
        info = classes.get(symbol)
        if info is None or not info.lookup_order or info.lookup_order[0] != symbol:
            return unknown("receiver has no verified MRO: " + symbol.text())
        chain = []
        for owner in info.lookup_order:
            base = classes.get(owner)
            if base is None:
                return unknown("incomplete MRO: " + owner.text())
            chain.append(base)
        # A subclass's fields or attribute hooks can redirect an inherited
        # method even when the class containing that method is immutable.
        if any(
            base.intercepted_lookup
            or (
                base.symbol != SymbolIdentity("builtins", "object")
                and any(
                    name in base.methods
                    or name in base.class_attributes
                    or name in base.descriptors
                    for name in ("__getattribute__", "__getattr__")
                )
            )
            for base in chain
        ):
            return unknown("receiver intercepts attribute lookup")
        if any(method in base.instance_fields for base in chain):
            return unknown("an instance field may shadow the method")
        resolved = None
        for base in chain:
            if method in base.descriptors:
                return unknown("method lookup reaches a descriptor")
            if method in base.class_attributes:
                return unknown("method lookup reaches a non-method class binding")
            resolved = base.methods.get(method)
            if resolved is not None:
                break
        if resolved is None:
            return unknown("method is not present in the verified MRO")
        if selected is not None and selected != resolved:
            return unknown("runtime receivers resolve different methods")
        selected = resolved
    if selected is None:
        return unknown("no method target")
    return CallIdentityDecision(
        "proven",
        "all admitted receivers resolve the same stable method",
        selected.target,
        selected.emitted_symbol,
        selected.kind,
        receiver.evidence,
        selected.abi,
    )


def declared_class_identities(host, verified_mros=None):
    """Read existing ClassInfo declarations without registering new classes.

    The legacy ``_resolve_method_mro`` can declare extern functions as a side
    effect. An observation must not do that. Use declarations already emitted
    by the common class lowerer and, for inheritance, a verified common MRO
    supplied by its caller. Never infer a C3 order from the old breadth-first
    lookup, and never derive a symbol or machine slot from a type annotation.

    A local undecorated root class has a trivial MRO. External metadata lacks
    a complete descriptor/class-binding inventory, so it remains a proof gap
    here. Receiver flow and lookup stability are still separate obligations.
    """
    result = {}
    verified_mros = verified_mros or {}
    lowering = getattr(host, "class_lowering", None)
    module = getattr(getattr(host, "ast_module", None), "name", "")
    declarations = getattr(lowering, "classes", {})
    for info in declarations.values():
        definition = getattr(info, "expanded_cd", None)
        if definition is None and getattr(info, "owning_module", None) is None:
            # During ordinary function emission, local methods are already
            # declared but emit_methods has not yet installed expanded_cd.
            # Recover only the exact local declaration from this module's AST.
            matches = [
                statement
                for statement in getattr(getattr(host, "ast_module", None), "body", ())
                if isinstance(statement, ClassDef) and statement.name == info.name
            ]
            if len(matches) == 1:
                definition = matches[0]
        if definition is None:
            continue
        owner = getattr(info, "owning_module", None) or module
        name = getattr(info, "export_class_name", None) or info.name
        symbol = SymbolIdentity(owner, name)
        if symbol in result:
            continue  # Registry aliases reference the same declaration.
        if (
            not owner
            or getattr(definition, "decorators", ())
            or getattr(definition, "keywords", ())
            or getattr(info, "runtime_decorators", ())
            or getattr(info, "metaclass_name", None)
        ):
            continue
        mro = verified_mros.get(symbol)
        if mro is None:
            # Even an explicit `object` base needs binding evidence: users
            # can shadow that name. Only the implicit root is trivial.
            if getattr(definition, "bases", ()) or getattr(info, "bases_ast", ()):
                continue
            mro = (symbol,)
        methods = {}
        declarations_complete = True
        for method, function in info.methods.items():
            function_type = getattr(function, "function_type", None)
            emitted_symbol = getattr(function, "name", None)
            if function_type is None or not emitted_symbol:
                declarations_complete = False
                break
            methods[method] = MethodIdentity(
                SymbolIdentity(owner, name + "." + method),
                str(emitted_symbol),
                info.method_kinds.get(method, "instance"),
                DeclaredCallABI(
                    str(function_type.return_type),
                    tuple(str(argument.type) for argument in function.args),
                    bool(function_type.var_arg),
                ),
            )
        if not declarations_complete:
            continue
        properties = (
            set(info.properties)
            | set(info.property_setters)
            | set(info.property_deleters)
        )
        # A method decorator can replace the function installed in the class
        # even though the undecorated function has a valid IR declaration.
        # A spelled 'staticmethod'/'classmethod' also needs binding proof;
        # preserve that gap instead of assuming the builtin decorator.
        for statement in getattr(definition, "body", ()):
            if getattr(statement, "decorators", ()):
                properties.add(statement.name)
        result[symbol] = ClassIdentity(
            symbol,
            tuple(mro),
            methods,
            frozenset(info.field_names),
            frozenset(properties),
            frozenset(info.class_attrs),
        )
    return result


def resolve_codegen_method_identity(host, receiver, method, verified_mros=None):
    """Join proven receiver facts to the common lowerer's real declarations.

    This does not manufacture receiver facts from ClassType or builder names.
    It is suitable for observation now and for a shared dispatch decision
    once the closure/flow proof producer is qualified.
    """
    if (
        not receiver.complete
        or not receiver.classes
        or not receiver.evidence
        or receiver.may_be_none
        or not receiver.lookup_stable
    ):
        return resolve_method_identity(receiver, method, {})
    classes = declared_class_identities(host, verified_mros)
    runtime_state = getattr(host, "_class_attr_runtime_state", {})
    if runtime_state:
        # The old map uses unqualified names. Do not turn an ambiguous name
        # collision into a proof that one class is unaffected by a write.
        for symbol in receiver.classes:
            info = classes.get(symbol)
            if info is None:
                continue
            for owner in info.lookup_order:
                keys = (
                    (owner.qualname, method),
                    (owner.module + "." + owner.qualname, method),
                    (owner.qualname, "__getattribute__"),
                    (owner.qualname, "__getattr__"),
                    (owner.module + "." + owner.qualname, "__getattribute__"),
                    (owner.module + "." + owner.qualname, "__getattr__"),
                )
                if any(key in runtime_state for key in keys):
                    return CallIdentityDecision(
                        "unknown",
                        "class lookup has runtime mutations",
                        evidence=receiver.evidence,
                    )
    return resolve_method_identity(receiver, method, classes)


class BindingIdentities:
    """Lexical bindings for an observer; an unknown local shadows its parent.

    The frontend is responsible for predeclaring every local binding in a
    function, including assignments appearing after a use. Copying an alias
    preserves its fact, whereas an unknown assignment clears it. Branches
    must join their environments before that environment is consumed.
    """

    def __init__(self, parent: BindingIdentities | None = None):
        self.parent = parent
        self.symbols: dict[str, SymbolIdentity | None] = {}
        self.receivers: dict[str, ReceiverIdentity] = {}

    def shadow(self, name: str) -> None:
        self.symbols[name] = None
        self.receivers[name] = ReceiverIdentity()

    def bind_symbol(self, name: str, symbol: SymbolIdentity) -> None:
        self.shadow(name)
        self.symbols[name] = symbol

    def bind_receiver(self, name: str, receiver: ReceiverIdentity) -> None:
        self.shadow(name)
        self.receivers[name] = receiver

    def symbol(self, name: str) -> SymbolIdentity | None:
        if name in self.symbols:
            return self.symbols[name]
        return self.parent.symbol(name) if self.parent is not None else None

    def receiver(self, name: str) -> ReceiverIdentity:
        if name in self.receivers:
            return self.receivers[name]
        return (
            self.parent.receiver(name)
            if self.parent is not None
            else ReceiverIdentity()
        )

    def assign_alias(self, target: str, source: str) -> None:
        symbol = self.symbol(source)
        receiver = self.receiver(source)
        self.shadow(target)
        self.symbols[target] = symbol
        self.receivers[target] = receiver

    def join(self, other: BindingIdentities) -> BindingIdentities:
        if self.parent is not other.parent:
            raise ValueError("cannot join different lexical scopes")
        result = BindingIdentities(self.parent)
        for name in self.symbols.keys() | other.symbols.keys():
            left, right = self.symbol(name), other.symbol(name)
            result.symbols[name] = left if left == right else None
            result.receivers[name] = self.receiver(name).join(other.receiver(name))
        return result
