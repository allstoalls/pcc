"""Small low-level IR data core for native pcc-Python hot paths.

Keep this file as a bootstrap-safe data layer.  It intentionally has no
dataclass decorators, no py_ast import, and no LLVM builder calls; Layer1 owns
the current AST-to-LowIR and LowIR-to-LLVM bridge while the representation
settles.
"""
from __future__ import annotations


LOW_I1 = 1
LOW_I64 = 2
LOW_PTR = 3
LOW_VOID = 4
LOW_F64 = 5


class LowValue:
    def __init__(self, ty: int):
        self.ty = ty


class LowConst(LowValue):
    def __init__(self, ty: int, value: int):
        LowValue.__init__(self, ty)
        self.value = value


class LowF64Const(LowValue):
    def __init__(self, value: float):
        LowValue.__init__(self, LOW_F64)
        self.value = value


class LowLocal(LowValue):
    def __init__(self, ty: int, name: str):
        LowValue.__init__(self, ty)
        self.name = name


class LowUnary(LowValue):
    def __init__(self, ty: int, op: str, value: LowValue):
        LowValue.__init__(self, ty)
        self.op = op
        self.value = value


class LowBinOp(LowValue):
    def __init__(self, ty: int, op: str, lhs: LowValue, rhs: LowValue):
        LowValue.__init__(self, ty)
        self.op = op
        self.lhs = lhs
        self.rhs = rhs


class LowCompare(LowValue):
    def __init__(self, ty: int, op: str, lhs: LowValue, rhs: LowValue):
        LowValue.__init__(self, ty)
        self.op = op
        self.lhs = lhs
        self.rhs = rhs


class LowSelect(LowValue):
    def __init__(
        self,
        ty: int,
        cond: LowValue,
        then_value: LowValue,
        else_value: LowValue,
    ):
        LowValue.__init__(self, ty)
        self.cond = cond
        self.then_value = then_value
        self.else_value = else_value


class LowCallDirect(LowValue):
    def __init__(
        self,
        ty: int,
        symbol: str,
        args: tuple,
        may_raise: bool = False,
        span=None,
    ):
        LowValue.__init__(self, ty)
        self.symbol = symbol
        self.args = args
        self.may_raise = may_raise
        self.span = span


class LowCallRuntime(LowValue):
    def __init__(
        self,
        ty: int,
        name: str,
        args: tuple,
        may_raise: bool = True,
        span=None,
    ):
        LowValue.__init__(self, ty)
        self.name = name
        self.args = args
        self.may_raise = may_raise
        self.span = span


class LowStoreLocal:
    def __init__(self, name: str, value: LowValue):
        self.name = name
        self.value = value


class LowEval:
    def __init__(self, value: LowValue):
        self.value = value


class LowBranch:
    def __init__(self, target: str):
        self.target = target


class LowCondBranch:
    def __init__(self, cond: LowValue, true_target: str, false_target: str):
        self.cond = cond
        self.true_target = true_target
        self.false_target = false_target


class LowReturn:
    def __init__(self, value):
        self.value = value


class LowBlock:
    def __init__(self, name: str, instrs=None, terminator=None):
        self.name = name
        self.instrs = [] if instrs is None else instrs
        self.terminator = terminator


class LowFunction:
    def __init__(
        self,
        name: str,
        symbol: str,
        params: tuple,
        return_ty: int,
        blocks: tuple,
        locals: tuple,
    ):
        self.name = name
        self.symbol = symbol
        self.params = params
        self.return_ty = return_ty
        self.blocks = blocks
        self.locals = locals


class LowBuilder:
    def __init__(self, name: str, symbol: str, params: tuple):
        self.name = name
        self.symbol = symbol
        self.params = params
        self.return_ty = LOW_I64
        self.blocks = []
        self.locals = []
        self._local_names = set()
        self._block_counter = 0
        self.current = self.new_block("entry")
        for param_name, param_ty in params:
            self.add_local(param_name, param_ty)

    def add_local(self, name: str, ty: int) -> None:
        if name in self._local_names:
            return
        self._local_names.add(name)
        self.locals.append((name, ty))

    def new_block(self, prefix: str) -> LowBlock:
        if not self.blocks:
            name = prefix
        else:
            self._block_counter += 1
            name = prefix + "." + str(self._block_counter)
        block = LowBlock(name=name)
        self.blocks.append(block)
        return block

    def position_at_end(self, block: LowBlock) -> None:
        self.current = block

    def terminated(self) -> bool:
        return self.current.terminator is not None

    def store(self, name: str, value: LowValue) -> None:
        self.add_local(name, value.ty)
        if self.current.terminator is None:
            self.current.instrs.append(LowStoreLocal(name=name, value=value))

    def eval(self, value: LowValue) -> None:
        if self.current.terminator is None:
            self.current.instrs.append(LowEval(value=value))

    def branch(self, target: LowBlock) -> None:
        if self.current.terminator is None:
            self.current.terminator = LowBranch(target=target.name)

    def cbranch(
        self,
        cond: LowValue,
        true_target: LowBlock,
        false_target: LowBlock,
    ) -> None:
        if self.current.terminator is None:
            self.current.terminator = LowCondBranch(
                cond=cond,
                true_target=true_target.name,
                false_target=false_target.name,
            )

    def ret(self, value) -> None:
        if self.current.terminator is None:
            self.current.terminator = LowReturn(value=value)

    def finish(self) -> LowFunction:
        return LowFunction(
            name=self.name,
            symbol=self.symbol,
            params=self.params,
            return_ty=self.return_ty,
            blocks=tuple(self.blocks),
            locals=tuple(self.locals),
        )
