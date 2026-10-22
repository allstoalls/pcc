"""Static pcc/pytest test-runner lowering helpers."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.method_decorators import public_method_name
from pcc.frontends.python.py_ast import (
    Assign,
    Attr,
    AugAssign,
    BoolLit,
    BoolType,
    Call,
    ClassDef,
    DynType,
    ExceptHandler,
    Expr,
    ExprStmt,
    FuncDef,
    FuncType,
    If,
    IntLit,
    IntType,
    ListExpr,
    Name,
    NoneType,
    SourceSpan,
    Stmt,
    StrLit,
    StrType,
    Try,
    TupleExpr,
    Type,
)


_I32 = ir.IntType(32)
_I64 = ir.IntType(64)


class StaticTestRunnerLoweringMixin:
    def _decorator_matches_any(self, dec: Expr, names: tuple[str, ...]) -> bool:
        qn = self._decorator_qualname(dec)
        return qn in names

    def _is_fixture_func(self, fd: FuncDef) -> bool:
        for dec in self._func_decorators(fd):
            if self._decorator_matches_any(
                dec,
                (
                    "fixture",
                    "pytest.fixture",
                    "pcc.test_runner.fixture",
                ),
            ):
                return True
        return False

    def _fixture_funcdefs(self) -> dict[str, FuncDef]:
        fixtures: dict[str, FuncDef] = {}
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef) and self._is_fixture_func(stmt):
                fixtures[stmt.name] = stmt
        return fixtures

    def _top_level_funcdef(self, name: str) -> Optional[FuncDef]:
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef) and stmt.name == name:
                return stmt
        return None

    def _parametrize_rows(self, fd: FuncDef) -> tuple[tuple[Expr, ...], ...]:
        rows_expr: Optional[Expr] = None
        for dec in self._func_decorators(fd):
            if not isinstance(dec, Call):
                continue
            qn = self._decorator_qualname(dec)
            if qn in ("parametrize", "pcc.test_runner.parametrize"):
                if len(dec.args) >= 1:
                    rows_expr = dec.args[0]
                    break
            elif qn == "pytest.mark.parametrize":
                if len(dec.args) >= 2:
                    rows_expr = dec.args[1]
                    break
        if rows_expr is None:
            return ()
        if not isinstance(rows_expr, (ListExpr, TupleExpr)):
            raise NotImplementedError(
                "pcc pytest parametrize requires a literal list/tuple of rows"
            )
        rows: list[tuple[Expr, ...]] = []
        for row in rows_expr.elems:
            if isinstance(row, (ListExpr, TupleExpr)):
                rows.append(tuple(row.elems))
            else:
                rows.append((row,))
        return tuple(rows)

    def _func_type_for_def(self, fd: FuncDef) -> FuncType:
        params = tuple(
            arg.annotation if isinstance(arg.annotation, Type) else DynType(name="dyn")
            for arg in fd.args
            if arg.name != ""
        )
        ret_ty = fd.return_ty if isinstance(fd.return_ty, Type) else NoneType(name="None")
        return FuncType(name="callable", params=params, ret=ret_ty)

    def _call_expr_for_funcdef(
        self,
        fd: FuncDef,
        args: tuple[Expr, ...],
        span: SourceSpan,
    ) -> Call:
        ret_ty = fd.return_ty if isinstance(fd.return_ty, Type) else NoneType(name="None")
        return Call(
            span=span,
            ty=ret_ty,
            func=Name(span=span, ty=self._func_type_for_def(fd), ident=fd.name),
            args=args,
            kwargs=(),
        )

    def _fixture_arg_exprs(
        self,
        fd: FuncDef,
        fixtures: dict[str, FuncDef],
        span: SourceSpan,
    ) -> tuple[Expr, ...]:
        args: list[Expr] = []
        for arg in fd.args:
            if arg.name == "":
                continue
            fixture_fd = fixtures.get(arg.name)
            if fixture_fd is None:
                raise NotImplementedError(
                    f"pcc pytest fixture {arg.name!r} is not defined"
                )
            args.append(self._call_expr_for_funcdef(fixture_fd, (), span))
        return tuple(args)

    def _static_test_call_exprs(
        self,
        fd: FuncDef,
        fixtures: dict[str, FuncDef],
        span: SourceSpan,
    ) -> tuple[Call, ...]:
        rows = self._parametrize_rows(fd)
        if rows:
            return tuple(self._call_expr_for_funcdef(fd, row, span) for row in rows)
        return (
            self._call_expr_for_funcdef(
                fd,
                self._fixture_arg_exprs(fd, fixtures, span),
                span,
            ),
        )

    def _emit_static_test_runner(
        self,
        funcs: tuple[FuncDef, ...],
        span: SourceSpan,
        *,
        exit_on_failure: bool,
    ) -> None:
        if not funcs:
            return
        fixtures = self._fixture_funcdefs()
        calls: list[Call] = []
        for fd in funcs:
            calls.extend(self._static_test_call_exprs(fd, fixtures, span))
        self._emit_static_test_runner_calls(
            tuple(calls),
            span,
            exit_on_failure=exit_on_failure,
        )

    def _emit_static_test_runner_calls(
        self,
        calls: tuple[Call, ...],
        span: SourceSpan,
        *,
        exit_on_failure: bool,
    ) -> None:
        if not calls:
            return
        self._emit_static_test_runner_groups(
            ((None, calls),), span, exit_on_failure=exit_on_failure, skip_exc=None,
        )

    def _emit_static_test_runner_groups(
        self,
        groups: tuple[tuple[Optional[str], tuple[Call, ...]], ...],
        span: SourceSpan,
        *,
        exit_on_failure: bool,
        skip_exc: Optional[Expr],
    ) -> None:
        """Run each group's calls, counting passes, failures and skips.

        A group named by a class is left out, its calls counted as skipped,
        when the class carries unittest's ``__unittest_skip__`` mark, as
        CPython's ``TestCase.run`` checks it. A call raising ``skip_exc`` is
        a skip. The summary names skips only when there were some.
        """
        int_ty = IntType(name="int")
        dyn = DynType(name="dyn")
        suffix = self._fresh("pytest_runner").replace(".", "_")
        passed_ref = Name(span=span, ty=int_ty, ident=f"__pcc_{suffix}_passed")
        failed_ref = Name(span=span, ty=int_ty, ident=f"__pcc_{suffix}_failed")
        skipped_ref = Name(span=span, ty=int_ty, ident=f"__pcc_{suffix}_skipped")
        one = IntLit(span=span, ty=int_ty, value=1)
        counts_skips = skip_exc is not None or any(name is not None for name, _calls in groups)
        counters = (passed_ref, failed_ref, skipped_ref) if counts_skips else (passed_ref, failed_ref)

        stmts: list[Stmt] = [
            Assign(
                span=span,
                targets=(counter,),
                value=IntLit(span=span, ty=int_ty, value=0),
                annotation=int_ty,
            )
            for counter in counters
        ]
        for class_name, calls in groups:
            cases: list[Stmt] = []
            for call in calls:
                handlers = [
                    ExceptHandler(
                        span=span,
                        exc_type=Name(span=span, ty=dyn, ident="AssertionError"),
                        name=None,
                        body=(AugAssign(span=span, target=failed_ref, op="+=", value=one),),
                    ),
                ]
                if skip_exc is not None:
                    handlers.append(ExceptHandler(
                        span=span,
                        exc_type=skip_exc,
                        name=None,
                        body=(AugAssign(span=span, target=skipped_ref, op="+=", value=one),),
                    ))
                cases.append(
                    Try(
                        span=span,
                        body=(
                            ExprStmt(span=span, expr=call),
                            AugAssign(span=span, target=passed_ref, op="+=", value=one),
                        ),
                        handlers=tuple(handlers),
                        else_body=(),
                        finally_body=(),
                    )
                )
            if class_name is None:
                stmts.extend(cases)
                continue
            marked = Call(
                span=span,
                ty=dyn,
                func=Name(span=span, ty=dyn, ident="getattr"),
                args=(
                    Name(span=span, ty=dyn, ident=class_name),
                    StrLit(span=span, ty=StrType(name="str"), value="__unittest_skip__"),
                    BoolLit(span=span, ty=BoolType(name="bool"), value=False),
                ),
                kwargs=(),
            )
            stmts.append(
                If(
                    span=span,
                    cond=marked,
                    body=(
                        AugAssign(
                            span=span,
                            target=skipped_ref,
                            op="+=",
                            value=IntLit(span=span, ty=int_ty, value=len(calls)),
                        ),
                    ),
                    else_body=tuple(cases),
                )
            )

        self._emit_stmts(tuple(stmts))
        passed = self._emit_expr_as_i64(passed_ref)
        failed = self._emit_expr_as_i64(failed_ref)
        summarized = None
        if counts_skips:
            skipped = self._emit_expr_as_i64(skipped_ref)
            with_skips = self.current_function.append_basic_block(
                name=self._fresh("pytest.summary.skips")
            )
            without_skips = self.current_function.append_basic_block(
                name=self._fresh("pytest.summary")
            )
            summarized = self.current_function.append_basic_block(
                name=self._fresh("pytest.summary.done")
            )
            self.builder.cbranch(
                self.builder.icmp_signed(
                    "!=", skipped, ir.Constant(_I64, 0), name=self._fresh("pytest.skipped"),
                ),
                with_skips,
                without_skips,
            )
            self.builder.position_at_end(with_skips)
            fmt = self._ptr_to_cstr(
                self._cstr_global(
                    "%ld passed, %ld failed, %ld skipped\n", ".fmt_pcc_pytest_skips",
                )
            )
            self.builder.call(self._printf, [fmt, passed, failed, skipped])
            self.builder.branch(summarized)
            self.builder.position_at_end(without_skips)
        fmt = self._ptr_to_cstr(
            self._cstr_global("%ld passed, %ld failed\n", ".fmt_pcc_pytest")
        )
        self.builder.call(self._printf, [fmt, passed, failed])
        if summarized is not None:
            self.builder.branch(summarized)
            self.builder.position_at_end(summarized)

        if exit_on_failure:
            is_fail = self.builder.icmp_signed(
                "!=",
                failed,
                ir.Constant(_I64, 0),
                name=self._fresh("pytest.failed"),
            )
            fail_bb = self.current_function.append_basic_block(
                name=self._fresh("pytest.exit")
            )
            cont_bb = self.current_function.append_basic_block(
                name=self._fresh("pytest.cont")
            )
            self.builder.cbranch(is_fail, fail_bb, cont_bb)
            self.builder.position_at_end(fail_bb)
            self.builder.call(self.runtime["py_process_exit"], [failed])
            if isinstance(self.current_function.function_type.return_type, ir.IntType):
                self.builder.ret(self.builder.trunc(failed, _I32))
            else:
                self.builder.ret_void()
            self.builder.position_at_end(cont_bb)

    def _run_tests_literal_funcdefs(self, expr: Call) -> Optional[tuple[FuncDef, ...]]:
        if expr.kwargs or len(expr.args) != 1:
            return None
        func = expr.func
        is_run_tests = False
        if isinstance(func, Name) and func.ident == "run_tests":
            is_run_tests = True
        elif isinstance(func, Attr):
            is_run_tests = (
                self._decorator_qualname(func) == "pcc.test_runner.run_tests"
            )
        if not is_run_tests:
            return None
        tests_expr = expr.args[0]
        if not isinstance(tests_expr, (ListExpr, TupleExpr)):
            return None
        funcs: list[FuncDef] = []
        for item in tests_expr.elems:
            if not isinstance(item, Name):
                return None
            fd = self._top_level_funcdef(item.ident)
            if fd is None:
                return None
            funcs.append(fd)
        return tuple(funcs)

    def _is_unittest_main_call(self, expr: Call) -> bool:
        if expr.args or expr.kwargs:
            return False
        func = expr.func
        return (
            isinstance(func, Attr)
            and func.name == "main"
            and isinstance(func.obj, Name)
            and func.obj.ident == "unittest"
        )

    def _is_unittest_testcase_base(self, base: Expr) -> bool:
        if isinstance(base, Name):
            return base.ident == "TestCase"
        return (
            isinstance(base, Attr)
            and base.name == "TestCase"
            and isinstance(base.obj, Name)
            and base.obj.ident == "unittest"
        )

    def _unittest_discovered_case_calls(
        self, span: SourceSpan,
    ) -> tuple[tuple[str, tuple[Call, ...]], ...]:
        """``ClassName().test_x()`` call exprs for every ``test*`` method on a
        top-level ``unittest.TestCase`` subclass, in source order, grouped by
        class name.

        ``unittest.main()`` discovers tests by reflecting over the running
        ``__main__`` module, which cannot see pcc-native classes; this static
        mirror plays the same role as the ``pytest.main()`` lowering."""
        dyn = DynType(name="dyn")
        groups: list[tuple[str, tuple[Call, ...]]] = []
        for stmt in self.ast_module.body:
            if not isinstance(stmt, ClassDef):
                continue
            if not any(self._is_unittest_testcase_base(b) for b in stmt.bases):
                continue
            calls: list[Call] = []
            for item in stmt.body:
                if not isinstance(item, FuncDef):
                    continue
                # A decorated test method is split by py_lift; its decorated
                # value is bound to the source name.
                case_name = public_method_name(item.name)
                if not case_name.startswith("test"):
                    continue
                inst = Call(
                    span=span,
                    ty=dyn,
                    func=Name(span=span, ty=dyn, ident=stmt.name),
                    args=(),
                    kwargs=(),
                )
                calls.append(
                    Call(
                        span=span,
                        ty=dyn,
                        func=Attr(span=span, ty=dyn, obj=inst, name=case_name),
                        args=(),
                        kwargs=(),
                    )
                )
            if calls:
                groups.append((stmt.name, tuple(calls)))
        return tuple(groups)

    def _is_pytest_main_call(self, expr: Call) -> bool:
        if expr.kwargs:
            return False
        func = expr.func
        return (
            isinstance(func, Attr)
            and func.name == "main"
            and isinstance(func.obj, Name)
            and func.obj.ident == "pytest"
        )

    def _pytest_discovered_funcdefs(self) -> tuple[FuncDef, ...]:
        funcs: list[FuncDef] = []
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef) and stmt.name.startswith("test_"):
                funcs.append(stmt)
        return tuple(funcs)

    def _maybe_emit_static_test_runner_stmt(self, expr: Call) -> bool:
        funcs = self._run_tests_literal_funcdefs(expr)
        if funcs is not None:
            self._emit_static_test_runner(
                funcs,
                expr.span,
                exit_on_failure=False,
            )
            return True
        if self._is_pytest_main_call(expr):
            self._emit_static_test_runner(
                self._pytest_discovered_funcdefs(),
                expr.span,
                exit_on_failure=True,
            )
            return True
        if self._is_unittest_main_call(expr):
            groups = self._unittest_discovered_case_calls(expr.span)
            if groups:
                dyn = DynType(name="dyn")
                skip_exc = Attr(
                    span=expr.span,
                    ty=dyn,
                    obj=Name(span=expr.span, ty=dyn, ident="unittest"),
                    name="SkipTest",
                )
                self._emit_static_test_runner_groups(
                    groups,
                    expr.span,
                    exit_on_failure=True,
                    skip_exc=skip_exc,
                )
                return True
        return False

    # -- Expression statement -----------------------------------------

