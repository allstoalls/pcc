"""Mutable LLVM-IR layer for pcc's IR passes.

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/include/llvm/IR/Instructions.h`` /
  ``/tmp/llvm-src/llvm-20.1.8.src/include/llvm/IR/BasicBlock.h`` /
  ``/tmp/llvm-src/llvm-20.1.8.src/include/llvm/IR/Function.h`` define
  the mutable C++ IR graph. llvmlite's ``binding`` layer exposes
  that graph read-only; llvmlite's ``ir`` layer is construction-only.
  For passes that need to clone blocks, rewire CFG, rename SSA
  values, or rewrite function signatures (argpromotion, loop-
  distribute, loop-vectorize, simple-loop-unswitch, slp-vectorize),
  this module fills the gap.

Design:

- :class:`MutableModule` parses an LLVM-IR text module into a
  line-oriented but block-structured representation:

    Module
      header_lines:   list[str]           # pre-function lines
      functions:      list[Function]
      tail_lines:     list[str]           # post-function lines

    Function
      header_line:    str                 # the ``define ...`` line
      arg_list:       list[Argument]
      blocks:         list[BasicBlock]

    BasicBlock
      name:           str
      label_line:     str                 # ``name:`` line verbatim
      instructions:   list[Instruction]
      terminator:     Instruction | None  # last instruction if
                                          # opcode ∈ terminator set

    Instruction
      text:           str                 # full text with trailing \n
      result_name:    str | None
      opcode:         str

- Mutation primitives:

  - :meth:`MutableModule.clone_block`: copy a basic block with a
    rename prefix applied to every defined SSA value.
  - :meth:`MutableModule.clone_blocks`: copy a set of blocks together
    with internal def-use remapped.
  - :meth:`MutableModule.rewrite_terminator`: replace a block's
    terminator.
  - :meth:`MutableModule.insert_instruction`: inject at a position.
  - :meth:`MutableModule.rename_value`: rename one SSA value
    everywhere.
  - :meth:`MutableModule.serialize`: emit back a valid IR text.

The representation is intentionally text-oriented — we do not build
a full SSA graph. The legacy pcc.ir_passes adapter provides external
LLVM verification for reference tests; this owned layer does not import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text_tokens import replace_local_names



# ---------------------------------------------------------------------------
# Regex toolkit
# ---------------------------------------------------------------------------


_DEFINE_HEADER_RE = re.compile(
    r"""
    ^(?P<prefix>\s*define\s+.+?\s+@)
    (?P<name>[\w\.]+)\s*
    \((?P<args>[^)]*)\)
    (?P<trailing>[^{\n]*)
    \s*\{\s*$
    """,
    re.VERBOSE,
)

_DECLARE_RE = re.compile(r"^\s*declare\s+")
_BLOCK_LABEL_RE = re.compile(r"^\s*([\w\.]+):\s*(?:;.*)?$")
_ASSIGN_RE = re.compile(r"^\s*%([\w\.]+)\s*=")
_OPCODE_RE = re.compile(r"^\s*(?:%[\w\.]+\s*=\s*)?(\w+)")

_TERMINATORS = {
    "ret", "br", "switch", "indirectbr", "invoke",
    "unreachable", "resume", "catchret", "catchswitch",
    "cleanupret",
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Argument:
    """One function argument: its type and SSA name."""

    ty: str
    name: str

    def serialize(self) -> str:
        # An unnamed parameter is legal IR and carries no SSA name: `...` in a
        # variadic signature, and a `define` whose body never refers to the
        # argument.  Inventing a name here rewrote `@Py_BuildValue(ptr %format,
        # ...)` as `(ptr %format, ... %anon1)`, which LLVM refuses to parse, so
        # a plain parse/serialize round trip corrupted every module holding a
        # variadic definition.
        if not self.name:
            return self.ty
        return f"{self.ty} %{self.name}"


@dataclass
class Instruction:
    """A single LLVM IR instruction."""

    text: str          # full line, including leading whitespace
    result_name: str | None = None
    opcode: str = ""

    @classmethod
    def from_text(cls, text: str) -> "Instruction":
        result = None
        m = _ASSIGN_RE.match(text)
        if m:
            result = m.group(1)
        op_m = _OPCODE_RE.match(text.lstrip())
        opcode = ""
        if op_m:
            opcode = op_m.group(1)
            # If the text starts with `%name = opcode ...`, opcode is
            # the SECOND word, not the first. _OPCODE_RE handles that
            # case via the leading optional assign prefix; when the
            # assign is present, we need to skip it.
            if text.lstrip().startswith("%") and " = " in text:
                # Re-extract.
                after_eq = text.split("=", 1)[1].lstrip()
                m2 = re.match(r"(\w+)", after_eq)
                if m2:
                    opcode = m2.group(1)
        return cls(text=text, result_name=result, opcode=opcode)

    def is_terminator(self) -> bool:
        return self.opcode in _TERMINATORS

    def operand_names(self) -> list[str]:
        """Return %SSA operand names referenced by this instruction.

        Skips the defined result's name so we only get true operands.
        """
        out: list[str] = []
        seen_result = False
        for name in re.findall(r"%([\w\.]+)", self.text):
            if not seen_result and name == self.result_name:
                seen_result = True
                continue
            out.append(name)
        return out


@dataclass
class BasicBlock:
    """A basic block: header line + instructions + terminator."""

    name: str
    label_line: str            # ``name:   ; preds = ...`` (if any)
    instructions: list[Instruction] = field(default_factory=list)

    @property
    def terminator(self) -> Instruction | None:
        return self.instructions[-1] if self.instructions else None

    def serialize(self) -> str:
        return self.label_line + "".join(i.text for i in self.instructions)


@dataclass
class Function:
    """A single function: define line, args, blocks."""

    header_line: str
    name: str
    args: list[Argument] = field(default_factory=list)
    trailing: str = ""         # text between ')' and '{' (attributes etc.)
    blocks: list[BasicBlock] = field(default_factory=list)
    footer_line: str = "}\n"

    def block(self, name: str) -> BasicBlock | None:
        for b in self.blocks:
            if b.name == name:
                return b
        return None

    def defined_names(self) -> set[str]:
        out: set[str] = set()
        for arg in self.args:
            if arg.name:
                out.add(arg.name)
        for b in self.blocks:
            for inst in b.instructions:
                if inst.result_name:
                    out.add(inst.result_name)
        return out

    def serialize(self) -> str:
        arg_text = ", ".join(a.serialize() for a in self.args)
        start = self.header_line.find("(")
        end = self.header_line.find(")", start + 1)
        header = self.header_line
        if start >= 0 and end >= 0:
            header = header[:start + 1] + arg_text + header[end:]
        parts = [header]
        for b in self.blocks:
            parts.append(b.serialize())
        parts.append(self.footer_line)
        return "".join(parts)


@dataclass
class MutableModule:
    """Mutable representation of an LLVM-IR module."""

    header_lines: list[str] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    declarations: list[str] = field(default_factory=list)
    globals_: list[str] = field(default_factory=list)
    tail_lines: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, ir_text: str) -> "MutableModule":
        return _parse_module(ir_text)

    def function(self, name: str) -> Function | None:
        for fn in self.functions:
            if fn.name == name:
                return fn
        return None

    def serialize(self) -> str:
        parts = []
        parts.extend(self.header_lines)
        parts.extend(self.globals_)
        parts.extend(self.declarations)
        for fn in self.functions:
            parts.append(fn.serialize())
        parts.extend(self.tail_lines)
        return "".join(parts)

    # -------------------------------------------------------------
    # Mutation primitives
    # -------------------------------------------------------------


    def rename_value_in_function(
        self, fn: Function, old: str, new: str
    ) -> None:
        """Rename a single SSA value within one function, everywhere."""
        replacements = {old: "%" + new}
        for block in fn.blocks:
            for inst in block.instructions:
                new_text = replace_local_names(inst.text, replacements)
                if new_text != inst.text:
                    inst.text = new_text
                    if inst.result_name == old:
                        inst.result_name = new

    def clone_block(
        self,
        fn: Function,
        block: BasicBlock,
        new_block_name: str,
        value_prefix: str,
    ) -> BasicBlock:
        """Return a deep copy of ``block`` with a new name and SSA prefix.

        Every SSA value defined in the clone gets ``value_prefix + "."``
        prepended to its name. Uses of in-block definitions are
        remapped to the prefixed names; uses of values defined outside
        the cloned block are left alone.
        """
        # Local renames: in-block def names → prefixed names.
        local_renames: dict[str, str] = {}
        for inst in block.instructions:
            if inst.result_name:
                local_renames[inst.result_name] = f"{value_prefix}.{inst.result_name}"

        replacements = {old: "%" + new for old, new in local_renames.items()}
        new_insts: list[Instruction] = []
        for inst in block.instructions:
            new_text = replace_local_names(inst.text, replacements)
            new_inst = Instruction(
                text=new_text,
                result_name=(
                    local_renames.get(inst.result_name)
                    if inst.result_name else None
                ),
                opcode=inst.opcode,
            )
            new_insts.append(new_inst)

        new_label_line = f"{new_block_name}:\n"
        return BasicBlock(
            name=new_block_name,
            label_line=new_label_line,
            instructions=new_insts,
        )

    def clone_blocks(
        self,
        fn: Function,
        blocks: list[BasicBlock],
        prefix: str,
    ) -> list[BasicBlock]:
        """Clone a set of blocks together.

        Intra-set references (both CFG label targets and SSA use-def)
        are remapped to the prefixed clones. References to values /
        blocks outside the set are left alone.
        """
        # Compute full renames.
        block_renames: dict[str, str] = {b.name: f"{prefix}.{b.name}" for b in blocks}
        value_renames: dict[str, str] = {}
        for b in blocks:
            for inst in b.instructions:
                if inst.result_name:
                    value_renames[inst.result_name] = f"{prefix}.{inst.result_name}"

        replacements = {old: "%" + new for old, new in value_renames.items()}
        for old, new in block_renames.items():
            replacements[old] = "%" + new
        cloned: list[BasicBlock] = []
        for b in blocks:
            new_insts: list[Instruction] = []
            for inst in b.instructions:
                new_text = replace_local_names(inst.text, replacements)
                new_inst = Instruction(
                    text=new_text,
                    result_name=(
                        value_renames.get(inst.result_name)
                        if inst.result_name else None
                    ),
                    opcode=inst.opcode,
                )
                new_insts.append(new_inst)
            new_label = f"{block_renames[b.name]}:\n"
            cloned.append(BasicBlock(
                name=block_renames[b.name],
                label_line=new_label,
                instructions=new_insts,
            ))
        return cloned

    def insert_blocks_before(
        self,
        fn: Function,
        anchor: str,
        blocks: list[BasicBlock],
    ) -> None:
        """Insert ``blocks`` into ``fn`` right before block named ``anchor``."""
        for i, b in enumerate(fn.blocks):
            if b.name == anchor:
                fn.blocks = fn.blocks[:i] + blocks + fn.blocks[i:]
                return
        fn.blocks.extend(blocks)

    def insert_blocks_after(
        self,
        fn: Function,
        anchor: str,
        blocks: list[BasicBlock],
    ) -> None:
        """Insert ``blocks`` into ``fn`` right after block named ``anchor``."""
        for i, b in enumerate(fn.blocks):
            if b.name == anchor:
                fn.blocks = fn.blocks[:i + 1] + blocks + fn.blocks[i + 1:]
                return
        fn.blocks.extend(blocks)

    def rewrite_terminator(
        self,
        block: BasicBlock,
        new_terminator_text: str,
    ) -> None:
        """Replace the last instruction of the block with new text."""
        if not block.instructions:
            return
        last = block.instructions[-1]
        inst = Instruction.from_text(new_terminator_text)
        last.text = inst.text
        last.opcode = inst.opcode
        last.result_name = inst.result_name

    def insert_instruction(
        self,
        block: BasicBlock,
        inst_text: str,
        *,
        before_terminator: bool = True,
    ) -> Instruction:
        """Insert an instruction. Default is right before the terminator."""
        inst = Instruction.from_text(inst_text)
        if before_terminator and block.instructions:
            block.instructions.insert(-1, inst)
        else:
            block.instructions.append(inst)
        return inst

    def replace_branch_target(
        self,
        block: BasicBlock,
        old_target: str,
        new_target: str,
    ) -> bool:
        """Rewrite any ``label %old_target`` in the block's terminator."""
        term = block.terminator
        if term is None:
            return False
        new_text = re.sub(
            r"label\s+%" + re.escape(old_target) + r"\b",
            f"label %{new_target}",
            term.text,
        )
        if new_text != term.text:
            term.text = new_text
            return True
        return False

    def strip_phi_incoming(
        self,
        block: BasicBlock,
        pred_name: str,
    ) -> None:
        """Remove any ``[val, %pred_name]`` from every phi in block."""
        pattern = re.compile(
            r"\[\s*[^,\]]+,\s*%" + re.escape(pred_name)
            + r"[ \t]*\][ \t]*,?[ \t]*"
        )
        for inst in block.instructions:
            if " = phi " not in inst.text:
                continue
            new_text = pattern.sub("", inst.text)
            new_text = re.sub(r",[ \t]*\n", "\n", new_text)
            inst.text = new_text


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _parse_module(ir_text: str) -> MutableModule:
    module = MutableModule()
    lines = ir_text.splitlines(keepends=True)
    i = 0
    n = len(lines)

    # Header: everything before the first define / declare / @.
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("define "):
            break
        if _DECLARE_RE.match(line):
            module.declarations.append(line)
            i += 1
            continue
        if stripped.startswith("@"):
            module.globals_.append(line)
            i += 1
            continue
        module.header_lines.append(line)
        i += 1

    # Functions.
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("define "):
            fn, consumed = _parse_function(lines, i)
            module.functions.append(fn)
            i += consumed
            continue
        if _DECLARE_RE.match(line):
            module.declarations.append(line)
            i += 1
            continue
        if stripped.startswith("@"):
            module.globals_.append(line)
            i += 1
            continue
        if not stripped:
            module.tail_lines.append(line)
            i += 1
            continue
        module.tail_lines.append(line)
        i += 1

    return module


def _parse_function(lines: list[str], start: int) -> tuple[Function, int]:
    """Parse one function starting at ``lines[start]``.

    Returns (function, lines_consumed).
    """
    header_line = lines[start]
    m = _DEFINE_HEADER_RE.match(header_line.rstrip("\n"))
    if not m:
        # Attempt a looser match — the header may span multiple lines.
        # For now treat the one-line form as the only supported case.
        raise ValueError(f"unrecognized define line: {header_line!r}")
    name = m.group("name")
    arg_text = m.group("args")
    trailing = m.group("trailing") or ""
    args: list[Argument] = []
    if arg_text.strip():
        for piece in _split_args(arg_text):
            piece = piece.strip()
            if not piece:
                continue
            # Normalize: find last `%name` as arg name, rest as type.
            mm = re.match(r"(.+?)\s+%([\w\.]+)\s*$", piece)
            if mm:
                args.append(Argument(ty=mm.group(1).strip(), name=mm.group(2)))
            else:
                # Unnamed argument (`...`, or a type with no `%name`): keep it
                # verbatim so serialization reproduces the signature.
                args.append(Argument(ty=piece, name=""))
    fn = Function(
        header_line=header_line,
        name=name,
        args=args,
        trailing=trailing,
    )

    i = start + 1
    current_block: BasicBlock | None = None
    # Entry block is implicit: first instructions before any label.
    current_block = BasicBlock(name="entry", label_line="entry:\n", instructions=[])
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped == "}":
            fn.footer_line = line
            i += 1
            break
        lm = _BLOCK_LABEL_RE.match(stripped)
        if lm:
            if current_block is not None and (
                current_block.instructions or current_block.name != "entry"
            ):
                fn.blocks.append(current_block)
            elif current_block is not None and current_block.name == "entry" \
                and not current_block.instructions:
                # Empty synthetic entry placeholder. LLVM functions may
                # legally start with any block label, not only "entry".
                pass
            current_block = BasicBlock(
                name=lm.group(1),
                label_line=line,
                instructions=[],
            )
            i += 1
            continue
        if current_block is not None and stripped:
            current_block.instructions.append(Instruction.from_text(line))
        elif current_block is not None:
            # Blank line inside a block — preserve as part of the last
            # instruction's trailing whitespace.
            if current_block.instructions:
                current_block.instructions[-1].text += line
            else:
                current_block.label_line += line
        i += 1
    if current_block is not None and (
        current_block.instructions or not fn.blocks
    ):
        fn.blocks.append(current_block)

    return fn, i - start


def _split_args(arg_text: str) -> list[str]:
    """Split a function argument list respecting parens / brackets."""
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in arg_text:
        if ch in "({<[":
            depth += 1
        elif ch in ")}>]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        out.append("".join(current))
    return out
