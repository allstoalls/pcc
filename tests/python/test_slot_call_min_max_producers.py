"""Iterable min/max retains its source and immediately publishes the winner."""
import re
import textwrap

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


@pytest.mark.parametrize("name,want_max", (("min", 0), ("max", 1)))
@pytest.mark.parametrize("site", ("return", "nested-binary"))
def test_iterable_extreme_publishes_owned_result_before_cleanup(name, want_max, site):
    expression = name + "(lexre.groupindex.values())"
    if site == "nested-binary":
        # Original Lexer._form_master_re producer shape at ply/lex.py:497.
        expression = "[None] * (" + expression + " + 1)"
    source = "def probe(lexre):\n    return " + expression + "\n"
    module = infer_module(parse_and_lift(source, "min_max.py", "min_max"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    body = re.search(r"^define [^\n]*@user_min_max_probe\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    body = body.group(0)
    calls = list(re.finditer(
        r"^  (%[^ ]+) = call [^\n]*@py_obj_min_max\(ptr [^,]+, i64 "
        + str(want_max) + r"\)\n", body, re.M,
    ))
    assert len(calls) == 1
    call = calls[0]
    following = body[call.end():].splitlines()[0].lstrip()
    assert following.startswith("store ptr " + call.group(1) + ", ptr "), following
    assert "@pcc_gc_foreign_lease_acquire(" in body
    assert "@pcc_gc_foreign_lease_release(" in body
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


PROGRAM = textwrap.dedent('''\
    import gc
    events = []

    class Item:
        def __init__(self, rank, label, broken=False):
            self.rank = rank
            self.label = label
            self.broken = broken
        def __lt__(self, other):
            gc.collect()
            events.append(('compare', self.label, other.label))
            if self.broken or other.broken:
                raise ValueError('comparison marker')
            return self.rank < other.rank
        def __del__(self):
            events.append(('drop', self.label))
            gc.collect()

    class Source:
        def __init__(self, values, label, fail_at=-1):
            self.values = values
            self.label = label
            self.fail_at = fail_at
            self.index = 0
        def __iter__(self):
            gc.collect()
            return self
        def __next__(self):
            gc.collect()
            events.append(('next', self.label, self.index))
            if self.index == self.fail_at:
                raise RuntimeError('iteration marker')
            if self.index == len(self.values):
                raise StopIteration
            value = self.values[self.index]
            self.index += 1
            return value
        def __del__(self):
            events.append(('source-drop', self.label))
            try:
                raise LookupError('disposal marker')
            except LookupError:
                pass
            gc.collect()

    # Untyped parameters deliberately select the generic object helper under
    # test instead of a statically specialized integer or user-class fold.
    def minimum(values):
        return min(values)
    def maximum(values):
        return max(values)
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def fail_later():
        gc.collect()
        raise RuntimeError('later marker')
    def temporary_source(label):
        return Source([Item(1, label + '-low'), Item(9, label + '-high')], label)
    def collect():
        gc.collect()
        gc.collect()

    def check_identity_and_comparisons():
        first = Item(7, 'first')
        tied = Item(7, 'tied')
        low = Item(-2, 'low')
        values = [first, low, tied]
        assert take(value=maximum(Source(values, 'identity-max'))) is first
        assert take(value=minimum(Source(values, 'identity-min'))) is low
        assert events.count(('compare', 'first', 'tied')) == 1
        assert events.count(('next', 'identity-max', 3)) == 1
        assert events.count(('next', 'identity-min', 3)) == 1
        huge = 1 << 100
        assert maximum([huge, huge + 1]) == huge + 1
        assert minimum([-huge, 0]) == -huge

    def check_errors():
        try:
            maximum(Source([], 'empty-max'))
        except ValueError:
            pass
        else:
            raise AssertionError('empty max did not raise')
        try:
            minimum(Source([], 'empty-min'))
        except ValueError:
            pass
        else:
            raise AssertionError('empty min did not raise')
        values = [Item(1, 'left'), Item(2, 'right', True), Item(3, 'unvisited')]
        try:
            maximum(Source(values, 'iteration-error', 1))
        except RuntimeError as error:
            assert str(error) == 'iteration marker'
        else:
            raise AssertionError('iterator error lost')
        assert events.count(('next', 'iteration-error', 2)) == 0
        try:
            maximum(Source(values, 'comparison-error-max'))
        except ValueError as error:
            assert str(error) == 'comparison marker'
        else:
            raise AssertionError('max comparison error lost')
        assert events.count(('next', 'comparison-error-max', 1)) == 1
        assert events.count(('next', 'comparison-error-max', 2)) == 0
        try:
            minimum(Source(values, 'comparison-error-min'))
        except ValueError as error:
            assert str(error) == 'comparison marker'
        else:
            raise AssertionError('min comparison error lost')
        assert events.count(('next', 'comparison-error-min', 1)) == 1
        assert events.count(('next', 'comparison-error-min', 2)) == 0

    def check_disposal():
        winner = take(value=maximum(temporary_source('temporary')))
        collect()
        assert winner.label == 'temporary-high'
        assert events.count(('source-drop', 'temporary')) == 1
        assert events.count(('drop', 'temporary-low')) == 1
        assert events.count(('drop', 'temporary-high')) == 0
        del winner
        collect()
        assert events.count(('drop', 'temporary-high')) == 1
        try:
            take(value=maximum(temporary_source('later-error')), later=fail_later())
        except RuntimeError as error:
            assert str(error) == 'later marker'
        else:
            raise AssertionError('later error lost')
        collect()
        assert events.count(('drop', 'later-error-low')) == 1
        assert events.count(('drop', 'later-error-high')) == 1

    def main():
        check_identity_and_comparisons()
        check_errors()
        check_disposal()
        print('MIN_MAX_PRODUCER_OWNERSHIP_OK')
    main()
''')


def test_min_max_native_control_reference(tmp_path):
    assert_reference_program(PROGRAM, "MIN_MAX_PRODUCER_OWNERSHIP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_min_max_native_five_gc(python_program_compiler, request,
                                explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "MIN_MAX_PRODUCER_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
