from __future__ import annotations

"""Target-neutral module-symbol preparation for the self backend."""

from dataclasses import dataclass, field

from .self_backend_ir import GlobalDef, ParsedFunction, TypeParseContext


@dataclass(frozen=True)
class PreparedModuleSymbols:
    internal_prefix: str
    defined_symbols: frozenset[str]
    internal_symbols: frozenset[str]
    thread_local_symbols: frozenset[str]
    target_triple: str = ""
    type_context: TypeParseContext | None = field(default=None, repr=False, compare=False)


def _stable_symbol_digest(text: str) -> str:
    """Return a deterministic 40-bit digest without a host hashlib edge."""
    # Keep every intermediate inside pcc's proven tagged-small-int lane.  The
    # self emitter is itself compiled by pcc, so a hash that intentionally
    # relies on u64 overflow would make its own symbol namespace depend on an
    # unproven bignum/value-lane transition.
    modulus = 1099511627776
    value = 0
    i = 0
    while i < len(text):
        value = (value * 131 + ord(text[i])) % modulus
        i += 1
    digits = "0123456789abcdef"
    out = ""
    shift = 36
    while shift >= 0:
        out += digits[(value >> shift) & 15]
        shift -= 4
    return out


def prepare_module_symbols(
    ir_text: str,
    globals_: list[GlobalDef],
    functions: list[ParsedFunction],
    target_triple: str = "",
) -> PreparedModuleSymbols:
    type_context = None
    for global_ in globals_:
        if global_.type_context is not None:
            type_context = global_.type_context
            break
    if type_context is None:
        for function in functions:
            if function.type_context is not None:
                type_context = function.type_context
                break
    if type_context is None:
        from .self_backend_parse import _parse_named_types
        type_context = _parse_named_types(ir_text)
    defined_symbols = frozenset(
        {global_.name for global_ in globals_ if global_.initializer} | {func.name for func in functions}
    )
    internal_symbols = frozenset(
        {global_.name for global_ in globals_ if global_.is_internal}
        | {func.name for func in functions if not func.is_global}
    )
    public_symbols = sorted(
        {global_.name for global_ in globals_ if global_.initializer and not global_.is_internal}
        | {func.name for func in functions if func.is_global}
    )
    if public_symbols:
        prefix_seed = "\n".join(public_symbols)
    else:
        prefix_seed = "\n".join(sorted(defined_symbols))
    internal_prefix = "__pccmod_" + _stable_symbol_digest(prefix_seed) + "_"
    return PreparedModuleSymbols(
        target_triple=target_triple,
        type_context=type_context,
        internal_prefix=internal_prefix,
        defined_symbols=defined_symbols,
        internal_symbols=internal_symbols,
        thread_local_symbols=frozenset(global_.name for global_ in globals_ if global_.tls_model),
    )
