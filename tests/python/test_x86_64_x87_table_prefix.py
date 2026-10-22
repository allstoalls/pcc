"""Dependency-replaced dispatch controls; no encoder, emitter or PCC imports.

Only the source-derived x87 prefix executes. Result construction and the
ordinary operand boundary are fakes. The operand-whitespace addition is
checked structurally here. These are not real encoding evidence.
"""
from __future__ import annotations

import ast
import copy

from tests.x86_64_operand_whitespace import assert_source_contract
from tests.x86_64_x87_reference import (
    leaf, old_function, prefix_length, source_tree, table_nodes,
)


class FakeError(Exception):
    pass


def _prefix_pair():
    tree = source_tree()
    outputs = []
    for old in (True, False):
        builds = []
        function = old_function(tree) if old else copy.deepcopy(leaf(tree))
        function.body = function.body[:prefix_length(function)]
        function.body += ast.parse("return _ordinary(mnemonic, rest, operand_cache)").body
        function.args = ast.parse("def probe(line, *, pc, labels, section_name, operand_cache=None): pass").body[0].args
        function.returns = None
        nodes = [function] if old else table_nodes(tree) + [function]
        for node in ast.walk(ast.Module(body=nodes, type_ignores=[])):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id in ("x87_fixed", "x87_stack", "_X87_FIXED", "_X87_STACK")):
                name = node.targets[0].id.lower().lstrip("_")
                node.value = ast.Call(func=ast.Name(id="_constructed", ctx=ast.Load()),
                                      args=[ast.Constant(name), node.value], keywords=[])

        def constructed(name, value, builds=builds):
            builds.append(name)
            return value

        scope = {
            "X86EncodeError": FakeError,
            "EncodedInstruction": lambda code: ("fake-result", code),
            "_ordinary": lambda mnemonic, rest, cache: ("fake-boundary", mnemonic, str(rest), cache),
            "_constructed": constructed,
        }
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, "<x87-prefix-only-fake>", "exec"), scope)
        outputs.append((scope["_encode_instruction_uncached"], builds))
    return outputs


def _call(function, line, cache=None):
    try:
        return function(line, pc=17, labels={"label": (".text", 31)},
                        section_name=".text", operand_cache=cache)
    except Exception as error:
        return (type(error), str(error))


def test_x87_prefix_fake_preserves_literal_results_and_boundaries():
    assert_source_contract()
    old, new = _prefix_pair()
    cache = object()
    lines = (
        "", "  ", "faddp", "fsubp", "fmulp", "fdivp", "fchs", "fabs",
        "fsqrt", "fprem", "frndint", "fldz", "fxch st(1)", "fld st(0)",
        "fstp st(0)", "fstp st(1)", "fucomip st(0), st(1)", "fnstsw ax",
        " FADDP  ", " FXCH  st(1)  ", "faddp rax", "faddp [", "fxch st(0)",
        "FXCH ST(1)", "fucomip st(0),st(1)", "mov rax, 42", "jne label",
        "mov rax, QWORD PTR data[rip]", "lock ret",
    )
    for line in lines:
        assert _call(old[0], line, cache) == _call(new[0], line, cache)
    assert _call(new[0], "faddp rax", cache) == (
        FakeError, "faddp takes no operands in the owned dialect",
    )
    assert _call(new[0], "fxch st(0)", cache) == (
        "fake-boundary", "fxch", "st(0)", cache,
    )


def test_x87_prefix_fake_counts_construction_at_its_original_control_points():
    old, new = _prefix_pair()
    assert old[1] == [] and new[1] == ["x87_fixed", "x87_stack"]
    for line in ("", "faddp", "faddp rax", "fxch st(1)", "jne label"):
        _call(old[0], line)
        _call(new[0], line)
    assert old[1] == ["x87_fixed", "x87_fixed", "x87_fixed", "x87_stack", "x87_fixed", "x87_stack"]
    assert new[1] == ["x87_fixed", "x87_stack"]


def test_x87_prefix_fake_preserves_normalization_and_two_stack_strip_calls():
    class Rest(str):
        def strip(self):
            events.append("rest.strip")
            return next(values)

    class Mnemonic(str):
        def lower(self):
            events.append("mnemonic.lower")
            return str(self).lower()

    class Normalized(str):
        def partition(self, separator):
            events.append(("partition", separator))
            return Mnemonic("FXCH"), " ", Rest("st(1)")

    class Line(str):
        def strip(self):
            events.append("line.strip")
            return Normalized("FXCH st(1)")

        def __hash__(self):
            raise AssertionError("line must not be hashed")

    for answers in (("st(1)", "st(1)"), ("st(1)", "st(0)")):
        observed = []
        for function, _builds in _prefix_pair():
            events = []
            values = iter(answers)
            observed.append((_call(function, Line("ignored")), events))
        assert observed[0] == observed[1]
        assert observed[0][1] == ["line.strip", ("partition", " "), "mnemonic.lower", "rest.strip", "rest.strip"]
        if answers[1] == "st(0)":
            assert observed[0][0][0] is KeyError


def test_x87_prefix_fake_preserves_fixed_error_before_stack_or_operand_work():
    for function, builds in _prefix_pair():
        before = len(builds)
        result = _call(function, "faddp [", object())
        assert result == (FakeError, "faddp takes no operands in the owned dialect")
        assert builds[before:] in ([], ["x87_fixed"])
