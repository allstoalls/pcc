"""Central registry for self-host module-name contracts in codegen.

The pcc compiler compiles its own runtime/codegen modules and must treat a
small set of them specially. Historically these were scattered direct
module-name comparisons across 17 files. This module
centralizes the sets as data so the special-casing is auditable and a single
place can document why each module is special.

Contracts:

- IR_SCAFFOLD_CONTRACT — modules compiled with ``--ir-scaffold=on`` even
  when the default mode is off.  These are the closed-world self-host core
  (runtime_abi, layer1, class_gen) whose lowering depends on the scaffold
  path; forcing it keeps the stage1/stage2/stage3 output identical.
- IR_PROVIDER_BINDING_CONTRACT — the owned IR implementation supplies the
  provider binding itself, without importing its own compat facade. This is
  only binding admission; rebinding and receiver/type checks still apply.
- CLASS_LOWERING_RECEIVER_CONTRACT — the class-lowering implementation has
  verified ClassLowering.parent and helper-parameter receiver facts. The
  field-contract tests check their constructors, writes and closed callers;
  this does not grant arbitrary parameters an IR receiver type.
- PY_AST_FIELD_OVERRIDE_MODULE — the py_ast dataclasses whose field ORDER is
  pinned by PY_AST_FIELD_NAME_OVERRIDES (see py_ast_contract.py); the pinned
  order must match the real dataclass field order byte-for-byte.
- L1_CODEGEN_HOST_ATTR_MODULE — layer1's L1CodeGen class carries extra host
  attributes appended to its field set.

These are compile-time data; do not add runtime module-name branches here.
"""

IR_SCAFFOLD_CONTRACT = "ir-scaffold-forced"
IR_PROVIDER_BINDING_CONTRACT = "ir-provider-binding"
CLASS_LOWERING_RECEIVER_CONTRACT = "class-lowering-receivers"
PY_AST_FIELD_ORDER_CONTRACT = "py-ast-field-order"
L1_CODEGEN_HOST_ATTR_CONTRACT = "l1-codegen-host-attrs"

PY_AST_FIELD_OVERRIDE_MODULE = "pcc.frontends.python.py_ast"
L1_CODEGEN_HOST_ATTR_MODULE = "pcc.frontends.python.codegen.layer1"

SELF_MODULE_CONTRACTS = {
    "pcc.ir.ir": (IR_PROVIDER_BINDING_CONTRACT,),
    "pcc.frontends.python.codegen.runtime_abi": (IR_SCAFFOLD_CONTRACT,),
    "pcc.frontends.python.codegen.layer1": (
        IR_SCAFFOLD_CONTRACT,
        L1_CODEGEN_HOST_ATTR_CONTRACT,
    ),
    "pcc.frontends.python.codegen.class_gen": (
        IR_SCAFFOLD_CONTRACT,
        CLASS_LOWERING_RECEIVER_CONTRACT,
    ),
    PY_AST_FIELD_OVERRIDE_MODULE: (PY_AST_FIELD_ORDER_CONTRACT,),
}


def module_has_contract(module_name: str | None, contract: str) -> bool:
    """Return whether a source module declares a codegen capability.

    Codegen sites ask for a semantic capability instead of naming the module
    that historically happened to require it.  The registry is also exported
    through the closed-world self-host metadata, so an extern class observes
    the same contract as the module currently being compiled.
    """

    if module_name is None:
        return False
    contracts = SELF_MODULE_CONTRACTS.get(module_name)
    if contracts is None:
        return False
    return contract in contracts


def module_for_class_symbol_contract(
    global_name: str,
    contract: str,
) -> str | None:
    """Resolve an encoded class-global owner by declared capability."""

    for module_name, contracts in SELF_MODULE_CONTRACTS.items():
        if contract not in contracts:
            continue
        prefix = ".class." + module_name.replace(".", "_") + "."
        if global_name.startswith(prefix):
            return module_name
    return None
