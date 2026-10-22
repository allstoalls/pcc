"""Sparse conditional constant propagation over the bootstrap SSA IR."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pcc.frontends.c.codegen.c_integer_fold_contract import FOLD_CONSTANT, fold_c_integer_binary as _fold_c_integer_binary, fold_c_integer_unary as _fold_c_integer_unary

from pcc.frontends.c.ssa.ir import SSABinaryOp, SSABlock, SSABranch, SSASwitch, SSACast, SSAConstant, SSAFieldAddr, SSAFunction, SSAJump, SSALoad, SSAStore, SSAParam, SSAPhi, SSAReturn, SSAUnaryOp, SSAUndef, SSAValue


class LatticeKind(str, Enum):
    UNKNOWN = "unknown"
    CONSTANT = "constant"
    OVERDEFINED = "overdefined"


@dataclass(frozen=True, slots=True)
class SSALatticeValue:
    kind: LatticeKind
    constant: int | None = None
    is_safe: bool = False

    @classmethod
    def unknown(cls) -> "SSALatticeValue":
        return cls(LatticeKind.UNKNOWN, None, False)

    @classmethod
    def overdefined(cls) -> "SSALatticeValue":
        return cls(LatticeKind.OVERDEFINED, None, False)

    @classmethod
    def constant_value(cls, value: int, *, is_safe: bool = False) -> "SSALatticeValue":
        return cls(LatticeKind.CONSTANT, value, is_safe)


@dataclass(slots=True)
class SSASCCPResult:
    function_name: str
    values: dict[str, SSALatticeValue] = field(default_factory=dict)
    reachable_blocks: set[str] = field(default_factory=set)
    folded_branches: dict[str, str] = field(default_factory=dict)

    def constant_value_names(self) -> dict[str, int]:
        return {
            name: value.constant
            for name, value in self.values.items()
            if value.kind == LatticeKind.CONSTANT and value.constant is not None
        }

    def safe_constant_value_names(self) -> dict[str, int]:
        """Return only `is_safe` constants — safe to emit verbatim in codegen.

        Unsafe folds are results of ops whose C semantics depend on
        signedness (ordered compares, +/-/* under unsigned wrap, etc.) —
        the lattice stores a Python int but using that value directly
        in an LLVM instruction would produce the wrong result for the
        unsigned wrap case.
        """
        return {
            name: value.constant
            for name, value in self.values.items()
            if (
                value.kind == LatticeKind.CONSTANT
                and value.constant is not None
                and value.is_safe
            )
        }

    def lattice_for(self, value: SSAValue | None) -> SSALatticeValue:
        if value is None:
            return SSALatticeValue.unknown()
        if isinstance(value, SSAConstant):
            return SSALatticeValue.constant_value(value.value, is_safe=value.is_safe)
        if isinstance(value, SSAUndef):
            return SSALatticeValue.unknown()
        if isinstance(value, SSAParam):
            return self.values.get(value.name, SSALatticeValue.overdefined())
        return self.values.get(value.name, SSALatticeValue.unknown())


class SSASCCPAnalyzer:
    """Run SCCP on the minimal bootstrap SSA representation."""

    # Supported pcc C targets are LP64. Unknown spellings deliberately remain
    # overdefined instead of inheriting Python's unbounded integer semantics.
    _INTEGER_TYPE_CONTRACTS = {
        "_Bool": (1, False),
        "char": (8, False),
        "signed char": (8, False),
        "unsigned char": (8, True),
        "short": (16, False),
        "unsigned short": (16, True),
        "int": (32, False),
        "unsigned int": (32, True),
        "long": (64, False),
        "unsigned long": (64, True),
        "long long": (64, False),
        "unsigned long long": (64, True),
        "__int128": (128, False),
        "unsigned __int128": (128, True),
    }

    def analyze(self, func: SSAFunction) -> SSASCCPResult:
        result = SSASCCPResult(function_name=func.name)
        result.reachable_blocks.add(func.entry_block)

        for param in func.params:
            result.values[param.name] = SSALatticeValue.overdefined()

        changed = True
        while changed:
            changed = False
            for block in func.blocks:
                if block.name not in result.reachable_blocks:
                    continue
                if self._visit_block(block, result):
                    changed = True

        return result

    def _visit_block(self, block: SSABlock, result: SSASCCPResult) -> bool:
        changed = False

        for instr in block.instructions:
            next_value = self._evaluate_instruction(instr, result, block)
            if self._update_value(result, instr.name, next_value):
                changed = True

        term = block.terminator
        if isinstance(term, SSAJump):
            if self._mark_reachable(result, term.target):
                changed = True
        elif isinstance(term, SSABranch):
            condition = self._value_state(term.condition, result)
            if (
                condition.kind == LatticeKind.CONSTANT
                and condition.constant is not None
                and condition.is_safe
            ):
                chosen = term.true_target if condition.constant else term.false_target
                result.folded_branches[block.name] = chosen
                if self._mark_reachable(result, chosen):
                    changed = True
            else:
                result.folded_branches.pop(block.name, None)
                for target in (term.true_target, term.false_target):
                    if self._mark_reachable(result, target):
                        changed = True
        elif isinstance(term, SSASwitch):
            value = self._value_state(term.value, result)
            if (
                value.kind == LatticeKind.CONSTANT
                and value.constant is not None
                and value.is_safe
            ):
                chosen = term.default_target
                for case_const, target in term.cases:
                    if case_const == value.constant:
                        chosen = target
                        break
                result.folded_branches[block.name] = chosen
                if self._mark_reachable(result, chosen):
                    changed = True
            else:
                result.folded_branches.pop(block.name, None)
                if self._mark_reachable(result, term.default_target):
                    changed = True
                for _, target in term.cases:
                    if self._mark_reachable(result, target):
                        changed = True
        elif isinstance(term, SSAReturn):
            pass

        return changed

    def _evaluate_instruction(
        self,
        instr,
        result: SSASCCPResult,
        block: SSABlock,
    ) -> SSALatticeValue:
        if isinstance(instr, SSAPhi):
            return self._evaluate_phi(instr, result, block)
        if isinstance(instr, SSABinaryOp):
            return self._evaluate_binary(instr, result)
        if isinstance(instr, SSACast):
            return self._evaluate_cast(instr, result)
        if isinstance(instr, SSAFieldAddr):
            return SSALatticeValue.overdefined()
        if isinstance(instr, SSALoad):
            return SSALatticeValue.overdefined()
        if isinstance(instr, SSAStore):
            return SSALatticeValue.overdefined()
        if isinstance(instr, SSAUnaryOp):
            return self._evaluate_unary(instr, result)
        return SSALatticeValue.overdefined()

    def _evaluate_phi(
        self,
        phi: SSAPhi,
        result: SSASCCPResult,
        block: SSABlock,
    ) -> SSALatticeValue:
        reachable_preds = {
            pred for pred in block.predecessors if pred in result.reachable_blocks
        }
        if not reachable_preds:
            return SSALatticeValue.unknown()

        current_constant: int | None = None
        saw_constant = False
        all_safe = True

        for pred_name, value in phi.incomings:
            if pred_name not in reachable_preds:
                continue
            lattice = self._value_state(value, result)
            if lattice.kind == LatticeKind.OVERDEFINED:
                return lattice
            if lattice.kind == LatticeKind.UNKNOWN:
                return lattice
            if lattice.constant is None:
                return SSALatticeValue.overdefined()
            if not saw_constant:
                current_constant = lattice.constant
                saw_constant = True
                all_safe = lattice.is_safe
                continue
            if lattice.constant != current_constant:
                return SSALatticeValue.overdefined()
            all_safe = all_safe and lattice.is_safe

        if saw_constant:
            return SSALatticeValue.constant_value(current_constant, is_safe=all_safe)
        return SSALatticeValue.unknown()

    def _evaluate_binary(
        self,
        instr: SSABinaryOp,
        result: SSASCCPResult,
    ) -> SSALatticeValue:
        left = self._value_state(instr.left, result)
        right = self._value_state(instr.right, result)

        if left.kind == LatticeKind.OVERDEFINED or right.kind == LatticeKind.OVERDEFINED:
            return SSALatticeValue.overdefined()
        if left.kind == LatticeKind.UNKNOWN or right.kind == LatticeKind.UNKNOWN:
            return SSALatticeValue.unknown()

        assert left.constant is not None
        assert right.constant is not None
        folded = self._fold_binary(instr, left.constant, right.constant)
        if folded is None:
            return SSALatticeValue.overdefined()
        return SSALatticeValue.constant_value(
            folded,
            is_safe=left.is_safe and right.is_safe,
        )

    def _evaluate_unary(
        self,
        instr: SSAUnaryOp,
        result: SSASCCPResult,
    ) -> SSALatticeValue:
        operand = self._value_state(instr.operand, result)
        if operand.kind == LatticeKind.OVERDEFINED:
            return SSALatticeValue.overdefined()
        if operand.kind == LatticeKind.UNKNOWN:
            return SSALatticeValue.unknown()

        assert operand.constant is not None
        folded = self._fold_unary(instr, operand.constant)
        if folded is None:
            return SSALatticeValue.overdefined()
        return SSALatticeValue.constant_value(
            folded,
            is_safe=operand.is_safe,
        )

    def _evaluate_cast(
        self,
        instr: SSACast,
        result: SSASCCPResult,
    ) -> SSALatticeValue:
        operand = self._value_state(instr.operand, result)
        if operand.kind != LatticeKind.CONSTANT or operand.constant is None:
            return operand
        contract = self._integer_type_contract(instr.type_name)
        if contract is None:
            return SSALatticeValue.overdefined()
        status, folded = _fold_c_integer_unary(
            "+", contract[0], contract[1], operand.constant
        )
        if status != FOLD_CONSTANT:
            return SSALatticeValue.overdefined()
        return SSALatticeValue.constant_value(folded, is_safe=operand.is_safe)

    def _value_state(
        self,
        value: SSAValue | None,
        result: SSASCCPResult,
    ) -> SSALatticeValue:
        return result.lattice_for(value)

    @classmethod
    def _fold_binary(
        cls, instr: SSABinaryOp, left: int, right: int
    ) -> int | None:
        if instr.op == "&&":
            return int(bool(left) and bool(right))
        if instr.op == "||":
            return int(bool(left) or bool(right))

        if instr.op in {"==", "!=", "<", "<=", ">", ">="}:
            contract = cls._usual_integer_contract(
                getattr(instr.left, "type_name", ""),
                getattr(instr.right, "type_name", ""),
            )
        elif instr.op in {"<<", ">>"}:
            contract = cls._promoted_integer_contract(
                getattr(instr.left, "type_name", "")
            )
        else:
            contract = cls._integer_type_contract(instr.type_name)
        if contract is None:
            return None
        status, folded = _fold_c_integer_binary(
            instr.op, contract[0], contract[1], left, right
        )
        return folded if status == FOLD_CONSTANT else None

    @classmethod
    def _fold_unary(cls, instr: SSAUnaryOp, operand: int) -> int | None:
        if instr.op == "!":
            return int(not operand)
        contract = cls._promoted_integer_contract(instr.type_name)
        if contract is None:
            return None
        status, folded = _fold_c_integer_unary(
            instr.op, contract[0], contract[1], operand
        )
        return folded if status == FOLD_CONSTANT else None

    @classmethod
    def _integer_type_contract(cls, type_name: str) -> tuple[int, bool] | None:
        normalized = cls._normalize_integer_type_name(type_name)
        return cls._INTEGER_TYPE_CONTRACTS.get(normalized)

    @classmethod
    def _promoted_integer_contract(cls, type_name: str) -> tuple[int, bool] | None:
        contract = cls._integer_type_contract(type_name)
        if contract is None:
            return None
        width, is_unsigned = contract
        if width < 32:
            return 32, False
        return width, is_unsigned

    @classmethod
    def _usual_integer_contract(
        cls, left_type_name: str, right_type_name: str
    ) -> tuple[int, bool] | None:
        left = cls._promoted_integer_contract(left_type_name)
        right = cls._promoted_integer_contract(right_type_name)
        if left is None or right is None:
            return None
        left_width, left_unsigned = left
        right_width, right_unsigned = right
        if left_unsigned == right_unsigned:
            return max(left_width, right_width), left_unsigned
        unsigned_width = left_width if left_unsigned else right_width
        signed_width = right_width if left_unsigned else left_width
        if unsigned_width >= signed_width:
            return unsigned_width, True
        # A wider signed type represents every value of the narrower unsigned
        # type on the supported two's-complement LP64 targets.
        return signed_width, False

    @staticmethod
    def _normalize_integer_type_name(type_name: str) -> str:
        tokens = [
            token
            for token in type_name.split()
            if token not in {"const", "volatile", "restrict", "register"}
        ]
        if not tokens:
            return ""
        if "signed" in tokens and len(tokens) > 1:
            tokens.remove("signed")
        joined = " ".join(tokens)
        aliases = {
            "signed": "int",
            "unsigned": "unsigned int",
            "int unsigned": "unsigned int",
            "short int": "short",
            "int short": "short",
            "unsigned short int": "unsigned short",
            "short unsigned": "unsigned short",
            "short unsigned int": "unsigned short",
            "long int": "long",
            "int long": "long",
            "unsigned long int": "unsigned long",
            "long unsigned": "unsigned long",
            "long unsigned int": "unsigned long",
            "long long int": "long long",
            "unsigned long long int": "unsigned long long",
            "long long unsigned": "unsigned long long",
            "long long unsigned int": "unsigned long long",
        }
        return aliases.get(joined, joined)

    @staticmethod
    def _mark_reachable(result: SSASCCPResult, block_name: str) -> bool:
        before = len(result.reachable_blocks)
        result.reachable_blocks.add(block_name)
        return len(result.reachable_blocks) != before

    @staticmethod
    def _update_value(
        result: SSASCCPResult,
        name: str,
        next_value: SSALatticeValue,
    ) -> bool:
        current = result.values.get(name, SSALatticeValue.unknown())
        merged = SSASCCPAnalyzer._merge_lattice(current, next_value)
        if merged == current:
            return False
        result.values[name] = merged
        return True

    @staticmethod
    def _merge_lattice(
        current: SSALatticeValue,
        next_value: SSALatticeValue,
    ) -> SSALatticeValue:
        if current.kind == LatticeKind.OVERDEFINED or next_value.kind == LatticeKind.UNKNOWN:
            return current
        if next_value.kind == LatticeKind.OVERDEFINED:
            return next_value
        if current.kind == LatticeKind.UNKNOWN:
            return next_value
        if next_value.kind == LatticeKind.CONSTANT and current.constant == next_value.constant:
            return SSALatticeValue.constant_value(
                current.constant,
                is_safe=current.is_safe and next_value.is_safe,
            )
        return SSALatticeValue.overdefined()
