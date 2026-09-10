"""Lower async context lifetimes through the ordinary try/finally machinery."""

from dataclasses import replace

from . import py_ast as pa


class _AsyncContexts:
    def __init__(self):
        self.serial = 0

    def name(self, span, role):
        self.serial += 1
        # '$' is not a source Python identifier, so user locals cannot collide.
        return pa.Name(span=span, ty=pa.DynType(name="dyn"), ident="__pcc_async$" + str(self.serial) + "_" + role)

    def call(self, span, function, args=()):
        return pa.Call(span=span, ty=pa.DynType(name="dyn"), func=function, args=args, kwargs=())

    def builtin(self, span, name, args):
        return self.call(span, pa.Name(span=span, ty=pa.DynType(name="dyn"), ident=name), args)

    def assign(self, name, value):
        return pa.Assign(span=name.span, targets=(name,), value=value)

    def block(self, body):
        out = []
        for stmt in body:
            if isinstance(stmt, (pa.FuncDef, pa.ClassDef)):
                stmt = replace(stmt, body=self.block(stmt.body))
            elif isinstance(stmt, (pa.If, pa.While, pa.For)):
                stmt = replace(stmt, body=self.block(stmt.body), else_body=self.block(stmt.else_body))
            elif isinstance(stmt, pa.Try):
                stmt = replace(stmt, body=self.block(stmt.body),
                    handlers=tuple(replace(h, body=self.block(h.body)) for h in stmt.handlers),
                    else_body=self.block(stmt.else_body), finally_body=self.block(stmt.finally_body))
            elif isinstance(stmt, pa.With):
                body = self.block(stmt.body)
                if not stmt.is_async:
                    stmt = replace(stmt, body=body)
                else:
                    for index in range(len(stmt.items) - 1, -1, -1):
                        context, target = stmt.items[index]
                        span = replace(stmt.span, col=stmt.span.col + index)
                        body = self.context(span, context, target, body)
                    out.extend(body)
                    continue
            out.append(stmt)
        return tuple(out)

    def context(self, span, expression, target, body):
        dyn = pa.DynType(name="dyn")
        manager = self.name(span, "manager")
        exit_method = self.name(span, "exit")
        entered = self.name(span, "entered")
        error = self.name(span, "error")
        error_type = self.name(span, "error_type")
        traceback = self.name(span, "traceback")
        caught = self.name(span, "caught")
        suppressed = self.name(span, "suppressed")
        none = pa.NoneLit(span=span, ty=pa.NoneType(name="None"))
        manager_type = self.builtin(span, "type", (manager,))
        exit_value = pa.Attr(span=span, ty=dyn, obj=manager_type, name="__aexit__")
        enter_value = pa.Attr(span=expression.span, ty=dyn, obj=manager_type, name="__aenter__")
        enter_call = self.call(expression.span, enter_value, (manager,))
        awaited_enter = self.builtin(expression.span, "__await__", (enter_call,))
        prefix = [self.assign(manager, expression), self.assign(exit_method, exit_value), self.assign(entered, awaited_enter)]
        if target is not None:
            prefix.append(pa.Assign(span=span, targets=(target,), value=entered))
        prefix.extend([self.assign(error, none), self.assign(error_type, none), self.assign(traceback, none)])
        handler = pa.ExceptHandler(span=span, exc_type=None, name=caught.ident, body=(
            self.assign(error, caught),
            self.assign(error_type, self.builtin(span, "type", (caught,))),
            self.assign(traceback, pa.Attr(span=span, ty=dyn, obj=caught, name="__traceback__")),
        ))
        exit_call = self.call(span, exit_method, (manager, error_type, error, traceback))
        exit_await = self.builtin(span, "__await__", (exit_call,))
        has_error = pa.Compare(span=span, ty=pa.BoolType(name="bool"), lhs=error, op="is not", rhs=none)
        not_suppressed = pa.UnaryOp(span=span, ty=pa.BoolType(name="bool"), op="not", operand=suppressed)
        propagate = pa.If(span=span, cond=has_error, body=(pa.If(span=span, cond=not_suppressed,
            body=(pa.Raise(span=span, exc=error, cause=None),)),))
        prefix.append(pa.Try(span=span, body=body, handlers=(handler,), else_body=(),
            finally_body=(self.assign(suppressed, exit_await), propagate)))
        return tuple(prefix)


def lower_async_contexts(module):
    return replace(module, body=_AsyncContexts().block(module.body))
