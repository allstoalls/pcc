"""Host-only observation of actual scaffold lowering, with no emitter change.

Use this around a retained codegen worker, not a text search over its source.
It records nested calls separately and observes the calls that the existing
lowering actually emits. No hook or diagnostic flag is added
to the production compiler or to the native bootstrap closure.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from typing import Callable

from pcc.frontends.python.codegen.call_identity import CallIdentityDecision, ReceiverIdentity, SymbolIdentity, resolve_codegen_method_identity, resolve_method_identity


def unresolved_codegen_identity(host, expression):
    """Report existing hints without upgrading them into unchecked proofs.

    Field/compat contracts enter through the resolver callback only after a
    closure checker has established their facts. A type annotation, a module
    name, and the old ``_ir_builder_env_flags`` are not that evidence.
    """
    receiver = getattr(getattr(expression, "func", expression), "obj", None)
    ty = getattr(receiver, "ty", None)
    module = getattr(ty, "module", "")
    name = getattr(ty, "name", "")
    evidence = []
    candidates = ()
    if module and name:
        candidates = (SymbolIdentity(module, name),)
        evidence.append("declared-type:" + candidates[0].text())
    ident = getattr(receiver, "ident", "")
    if ident and getattr(host, "_ir_builder_env_flags", {}).get(ident, False):
        evidence.append("legacy-constructor-hint:" + ident)
    fact = ReceiverIdentity(
        classes=candidates,
        # Neither an annotation nor the legacy constructor-name heuristic
        # establishes all possible values at this program point.
        complete=False,
        lookup_stable=False,
        evidence=tuple(evidence),
    )
    attribute = getattr(expression, "func", expression)
    return resolve_method_identity(fact, getattr(attribute, "name", ""), {})


class CodegenIdentityResolver:
    """Observe verified flow facts using actual common-codegen declarations.

    The producer receives the real receiver AST at the call site. Returning
    None reports a gap; naming a variable 'builder' is never a proof. No
    temporary class/function declarations are installed by this adapter.
    """

    def __init__(self, receiver_facts: Callable, verified_mros: Callable | None = None):
        self.receiver_facts = receiver_facts
        self.verified_mros = verified_mros

    def __call__(self, host, expression):
        attribute = getattr(expression, "func", None)
        if attribute is None:
            return CallIdentityDecision(
                "unknown", "provider value binding is not proved"
            )
        receiver = getattr(attribute, "obj", None)
        if receiver is None:
            return unresolved_codegen_identity(host, expression)
        fact = self.receiver_facts(host, receiver)
        if fact is None:
            return unresolved_codegen_identity(host, expression)
        mros = self.verified_mros(host) if self.verified_mros is not None else None
        return resolve_codegen_method_identity(host, fact, attribute.name, mros)


class ScaffoldDecisionRecorder:
    def __init__(
        self,
        resolve_identity: Callable = unresolved_codegen_identity,
        write_record: Callable | None = None,
    ):
        self.resolve_identity = resolve_identity
        self.write_record = write_record
        self.records: list[dict] = []
        self._active: list[tuple[object, dict]] = []
        self._occurrences: Counter = Counter()

    def begin(self, host, expression, kind, member):
        span = getattr(expression, "span", None)
        module = getattr(host.ast_module, "name", "")
        function = getattr(getattr(host, "current_func_def", None), "name", "")
        point = (
            module,
            function,
            getattr(span, "line", 0),
            getattr(span, "col", 0),
            kind,
            member,
        )
        ordinal = self._occurrences[point]
        self._occurrences[point] += 1
        decision = self.resolve_identity(host, expression)
        record = {
            "schema": "pcc.scaffold-decision.v1",
            "execution_owner": "host-cpython-observer",
            "module": module,
            "function": function,
            "file": getattr(span, "file", ""),
            "line": getattr(span, "line", 0),
            "column": getattr(span, "col", 0),
            "occurrence": ordinal,
            "kind": kind,
            "member": member,
            "scaffold_mode": host.ir_scaffold_mode,
            "resolution": decision.status,
            "reason": decision.reason,
            "evidence": list(decision.evidence),
            "target": decision.target.text() if decision.target else None,
            "proposed_emitted_symbol": decision.emitted_symbol,
            "method_kind": decision.method_kind,
            "proposed_abi": (
                {
                    "return_type": decision.abi.return_type,
                    "parameter_types": list(decision.abi.parameter_types),
                    "var_arg": decision.abi.var_arg,
                }
                if decision.abi is not None
                else None
            ),
            "legacy_callees": [],
            "legacy_value_globals": [],
            "completed": False,
        }
        self._active.append((host, record))
        return record

    def emitted_call(self, function, arguments):
        if not self._active or function is None:
            return
        symbol = str(getattr(function, "name", ""))
        if not symbol.startswith("user_pcc_ir_ir_"):
            return
        function_type = function.function_type
        self._active[-1][1]["legacy_callees"].append(
            {
                "symbol": symbol,
                "return_type": str(function_type.return_type),
                "parameter_types": [str(argument.type) for argument in function.args],
                "argument_types": (
                    [str(argument.type) for argument in arguments]
                    if isinstance(arguments, (list, tuple))
                    else None
                ),
                "var_arg": bool(function_type.var_arg),
            }
        )

    def loaded_value(self, pointer):
        if not self._active:
            return
        name = str(getattr(pointer, "name", ""))
        if name.startswith(".class.pcc_ir_ir."):
            self._active[-1][1]["legacy_value_globals"].append(name)

    def finish(self, record, error=None):
        _host, active = self._active.pop()
        if active is not record:
            raise RuntimeError("scaffold observer call stack is inconsistent")
        if error is not None:
            record["error"] = type(error).__name__ + ": " + str(error)
        else:
            record["completed"] = True
        expected = record["proposed_emitted_symbol"]
        record["same_emitted_target"] = (
            record["completed"]
            and record["resolution"] == "proven"
            and expected is not None
            and (
                any(item["symbol"] == expected for item in record["legacy_callees"])
                or expected in record["legacy_value_globals"]
            )
        )
        matching_calls = [
            item for item in record["legacy_callees"] if item["symbol"] == expected
        ]
        proposed_abi = record["proposed_abi"]
        record["same_declared_abi"] = bool(
            record["same_emitted_target"]
            and proposed_abi is not None
            and len(matching_calls) == 1
            and all(
                matching_calls[0].get(key) == value
                for key, value in proposed_abi.items()
            )
        )
        self.records.append(record)
        if self.write_record is not None:
            self.write_record(record)

    def summary(self):
        return {
            "schema": "pcc.scaffold-decision-summary.v1",
            "records": len(self.records),
            "completed": sum(bool(row["completed"]) for row in self.records),
            "same_emitted_target": sum(
                bool(row["same_emitted_target"]) for row in self.records
            ),
            "unknown": sum(row["resolution"] != "proven" for row in self.records),
            "same_declared_abi": sum(
                bool(row["same_declared_abi"]) for row in self.records
            ),
            "reasons": dict(
                sorted(Counter(row["reason"] for row in self.records).items())
            ),
        }


_OBSERVER_ACTIVE = False


@contextmanager
def observe_scaffold_decisions(recorder: ScaffoldDecisionRecorder):
    """Temporarily wrap host codegen; preserve return values and exceptions.

    This is deliberately single-process. The command-line tool replays the
    actual worker manifest in that process instead of silently missing child
    workers from a multi-process Stage1 run.
    """
    global _OBSERVER_ACTIVE
    if _OBSERVER_ACTIVE:
        raise RuntimeError("nested scaffold observation is not supported")
    from pcc.frontends.python.codegen.ir_scaffold_lowering import IrScaffoldLoweringMixin
    from pcc.ir import ir

    scaffold = IrScaffoldLoweringMixin
    original_call = scaffold._maybe_emit_ir_scaffold_call
    original_value = scaffold._maybe_emit_ir_scaffold_symbol_value
    original_ir_call = ir.IRBuilder.call
    original_ir_load = ir.IRBuilder.load

    def call(host, expression):
        attribute = expression.func
        method = host._ir_scaffold_candidate(attribute)
        symbol = host._ir_module_symbol_target(attribute) if method is None else None
        if method is None and symbol is None:
            return original_call(host, expression)
        row = recorder.begin(
            host, expression, "method" if method else "constructor", method or symbol
        )
        try:
            result = original_call(host, expression)
        except BaseException as error:
            recorder.finish(row, error)
            raise
        recorder.finish(row)
        return result

    def value(host, expression):
        # An ir.Type / ir.Undefined load is also a provider dependency. Do
        # not drop it from the migration baseline because it is not a call.
        obj = getattr(expression, "obj", None)
        if not host._ir_scaffold_enabled() or getattr(obj, "ident", None) != "ir":
            return original_value(host, expression)
        row = recorder.begin(host, expression, "symbol-value", expression.name)
        try:
            result = original_value(host, expression)
        except BaseException as error:
            recorder.finish(row, error)
            raise
        row["lowered"] = result is not None
        recorder.finish(row)
        return result

    def emitted_call(builder, *args, **kwargs):
        result = original_ir_call(builder, *args, **kwargs)
        function = args[0] if args else kwargs.get("fn")
        arguments = args[1] if len(args) > 1 else kwargs.get("args")
        recorder.emitted_call(function, arguments)
        return result

    def loaded_value(builder, *args, **kwargs):
        result = original_ir_load(builder, *args, **kwargs)
        recorder.loaded_value(args[0] if args else kwargs.get("ptr"))
        return result

    _OBSERVER_ACTIVE = True
    scaffold._maybe_emit_ir_scaffold_call = call
    scaffold._maybe_emit_ir_scaffold_symbol_value = value
    ir.IRBuilder.call = emitted_call
    ir.IRBuilder.load = loaded_value
    try:
        yield recorder
    finally:
        scaffold._maybe_emit_ir_scaffold_call = original_call
        scaffold._maybe_emit_ir_scaffold_symbol_value = original_value
        ir.IRBuilder.call = original_ir_call
        ir.IRBuilder.load = original_ir_load
        _OBSERVER_ACTIVE = False


def compare_decision_records(before: list[dict], after: list[dict]) -> list[dict]:
    """Compare individual call sites, including their actual ABI declarations.

    A stable total call count is not equivalence. Replacing one target with
    another, losing a site, or changing ptr/i64 at the same symbol is visible.
    Caller/source identities belong in the surrounding replay receipt.
    """
    fields = ("module", "function", "line", "column", "occurrence", "kind", "member")

    def index(rows):
        result = {}
        for row in rows:
            key = tuple(row[field] for field in fields)
            if key in result:
                raise ValueError("duplicate scaffold decision point: " + repr(key))
            result[key] = row
        return result

    left, right = index(before), index(after)
    differences = []
    for key in sorted(left.keys() | right.keys()):
        a, b = left.get(key), right.get(key)
        if a is None or b is None:
            differences.append(
                {"point": list(key), "change": "added" if a is None else "removed"}
            )
            continue
        for field in (
            "completed",
            "legacy_callees",
            "legacy_value_globals",
            "lowered",
            "method_kind",
        ):
            if a.get(field) != b.get(field):
                differences.append(
                    {
                        "point": list(key),
                        "change": field,
                        "before": a.get(field),
                        "after": b.get(field),
                    }
                )
        if not a["completed"] or not b["completed"]:
            differences.append({"point": list(key), "change": "incomplete-lowering"})
        if b["resolution"] != "proven":
            differences.append(
                {"point": list(key), "change": "unproved", "reason": b["reason"]}
            )
        elif b["kind"] == "symbol-value":
            differences.append(
                {"point": list(key), "change": "value-dependency-proof-required"}
            )
        elif not b["same_emitted_target"]:
            differences.append(
                {
                    "point": list(key),
                    "change": "different-target-or-adapter",
                    "target": b["target"],
                }
            )
        elif not b.get("same_declared_abi", False):
            differences.append(
                {"point": list(key), "change": "declared-abi-not-proved"}
            )
    return differences
