"""Minimal SSA IR used by the MidTier roadmap bootstrap."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SSAValue:
    name: str
    type_name: str = "int"


@dataclass(slots=True)
class SSAParam(SSAValue):
    source_name: str = ""


@dataclass(slots=True)
class SSAConstant(SSAValue):
    value: int = 0
    is_safe: bool = False

    @classmethod
    def from_int(
        cls,
        value: int,
        type_name: str = "int",
        *,
        is_safe: bool = False,
    ) -> "SSAConstant":
        return cls(name=str(value), type_name=type_name, value=value, is_safe=is_safe)


@dataclass(slots=True)
class SSAStringConstant(SSAValue):
    value: str = ""
    literal_kind: str = "string"


@dataclass(slots=True)
class SSAGlobalRef(SSAValue):
    symbol_name: str = ""


@dataclass(slots=True)
class SSAUndef(SSAValue):
    source_name: str = ""


@dataclass(slots=True)
class SSABinding:
    kind: str
    target_name: str
    value: SSAValue
    source_coord: str | None = None
    block_name: str = ""
    type_name: str = ""


@dataclass(slots=True)
class SSAInstruction(SSAValue):
    source_coord: str | None = None
    available_bindings: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(slots=True)
class SSAUnaryOp(SSAInstruction):
    op: str = ""
    operand: SSAValue | None = None


@dataclass(slots=True)
class SSACast(SSAInstruction):
    operand: SSAValue | None = None


@dataclass(slots=True)
class SSALoad(SSAInstruction):
    base: SSAValue | None = None
    index: SSAValue | None = None


@dataclass(slots=True)
class SSAFieldAddr(SSAInstruction):
    base: SSAValue | None = None
    field_name: str = ""
    access_kind: str = "->"


@dataclass(slots=True)
class SSAFieldExtract(SSAInstruction):
    base: SSAValue | None = None
    field_name: str = ""


@dataclass(slots=True)
class SSAStore(SSAInstruction):
    addr: SSAValue | None = None
    value: SSAValue | None = None


@dataclass(slots=True)
class SSAStackAlloc(SSAInstruction):
    elem_type_name: str = "int"
    count: int = 1


@dataclass(slots=True)
class SSABinaryOp(SSAInstruction):
    op: str = ""
    left: SSAValue | None = None
    right: SSAValue | None = None


@dataclass(slots=True)
class SSACall(SSAInstruction):
    callee_name: str = ""
    callee: SSAValue | None = None
    args: tuple[SSAValue, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class SSAPhi(SSAInstruction):
    variable_name: str = ""
    incomings: list[tuple[str, SSAValue]] = field(default_factory=list)


@dataclass(slots=True)
class SSATerminator:
    pass


@dataclass(slots=True)
class SSAJump(SSATerminator):
    target: str


@dataclass(slots=True)
class SSABranch(SSATerminator):
    condition: SSAValue
    true_target: str
    false_target: str
    source_coord: str | None = None


@dataclass(slots=True)
class SSAReturn(SSATerminator):
    value: SSAValue | None = None
    source_coord: str | None = None


@dataclass(slots=True)
class SSASwitch(SSATerminator):
    """C-style switch terminator: value dispatched to exact-int cases.

    LLVM reference: SwitchInst in
    /tmp/llvm-src/.../include/llvm/IR/Instructions.h — holds a value,
    a default target, and a list of (constant-value, target) pairs.
    """
    value: SSAValue | None = None
    default_target: str = ""
    cases: tuple[tuple[int, str], ...] = ()
    source_coord: str | None = None


@dataclass(slots=True)
class SSABlock:
    name: str
    instructions: list[SSAInstruction] = field(default_factory=list)
    terminator: SSATerminator | None = None
    predecessors: list[str] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)

    def append(self, instruction: SSAInstruction) -> SSAInstruction:
        self.instructions.append(instruction)
        return instruction


@dataclass(slots=True)
class SSAFunction:
    name: str
    params: list[SSAParam]
    blocks: list[SSABlock]
    entry_block: str
    bindings: list[SSABinding] = field(default_factory=list)
    dominators: dict[str, set[str]] = field(default_factory=dict)
    immediate_dominators: dict[str, str | None] = field(default_factory=dict)

    def block(self, name: str) -> SSABlock:
        for block in self.blocks:
            if block.name == name:
                return block
        raise KeyError(name)

    def instruction(self, name: str) -> SSAInstruction:
        for block in self.blocks:
            for instruction in block.instructions:
                if instruction.name == name:
                    return instruction
        raise KeyError(name)

    def recompute_cfg(self) -> None:
        block_map = {block.name: block for block in self.blocks}
        for block in self.blocks:
            block.predecessors.clear()
            block.successors.clear()

        for block in self.blocks:
            successors = []
            term = block.terminator
            if isinstance(term, SSAJump):
                successors = [term.target]
            elif isinstance(term, SSABranch):
                successors = [term.true_target, term.false_target]
            elif isinstance(term, SSASwitch):
                successors = [term.default_target] + [tgt for _, tgt in term.cases]

            for target in successors:
                if target not in block_map:
                    raise KeyError(f"unknown block target {target!r}")
                if target not in block.successors:
                    block.successors.append(target)
                pred_list = block_map[target].predecessors
                if block.name not in pred_list:
                    pred_list.append(block.name)

    def reachable_blocks(self) -> set[str]:
        seen: set[str] = set()
        worklist = [self.entry_block]
        while worklist:
            name = worklist.pop()
            if name in seen:
                continue
            seen.add(name)
            worklist.extend(self.block(name).successors)
        return seen

    def recompute_dominators(self) -> None:
        self.recompute_cfg()
        reachable = self.reachable_blocks()
        if self.entry_block not in reachable:
            raise ValueError(f"entry block {self.entry_block!r} is unreachable")

        block_names = [block.name for block in self.blocks]
        all_reachable = set(reachable)
        dom: dict[str, set[str]] = {}

        for name in block_names:
            if name == self.entry_block:
                dom[name] = {name}
            elif name in reachable:
                dom[name] = set(all_reachable)
            else:
                dom[name] = {name}

        changed = True
        while changed:
            changed = False
            for name in block_names:
                if name == self.entry_block or name not in reachable:
                    continue
                block = self.block(name)
                preds = [pred for pred in block.predecessors if pred in reachable]
                if not preds:
                    new_dom = {name}
                else:
                    new_dom = set(all_reachable)
                    for pred in preds:
                        new_dom &= dom[pred]
                    new_dom.add(name)
                if new_dom != dom[name]:
                    dom[name] = new_dom
                    changed = True

        idom: dict[str, str | None] = {}
        for name in block_names:
            if name not in reachable or name == self.entry_block:
                idom[name] = None
                continue
            candidates = dom[name] - {name}
            immediate = None
            for candidate in candidates:
                if all(
                    other == candidate or other in dom[candidate]
                    for other in candidates
                ):
                    immediate = candidate
                    break
            idom[name] = immediate

        self.dominators = dom
        self.immediate_dominators = idom
