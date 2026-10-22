"""Set algebra method binding, with the original compiler generator-splat shape."""
import contextlib
import io
import os
import re
import subprocess

import pytest

from pcc.frontends.python.pipeline import compile_python


UNION = '''def main():
    groups = [{"a", "b"}, {"b", "c"}, set()]
    result = set().union(*(names for names in groups))
    assert result == {"a", "b", "c"}
    idx = 1
    other_written = set().union(*(names for j, names in enumerate(groups) if j != idx))
    assert other_written == {"a", "b"}
    source = {1, 2}
    assert source.union() == source and source.union() is not source
    assert source.union(*()) == source
    assert source.union([2, 3], (4,), {5: 6}) == {1, 2, 3, 4, 5}
    assert source.union(*([3], [4]), *([5],)) == {1, 2, 3, 4, 5}
    assert source == {1, 2}
    print("SET_UNPACK_OK")
main()
'''

METHODS = '''def main():
    source = {1, 2, 3, 4}
    assert source.intersection() == source and source.intersection() is not source
    assert source.difference() == source and source.difference() is not source
    assert source.intersection(*([2, 3, 4], (3, 4, 5))) == {3, 4}
    assert source.difference(*([1], (2,), {4: 0})) == {3}
    assert source.symmetric_difference(*([3, 3, 5, 5],)) == {1, 2, 4, 5}
    alias = source
    assert source.update(*()) is None
    assert source.update(*([5], (6,))) is None
    assert alias == {1, 2, 3, 4, 5, 6}
    assert source.intersection_update(*([2, 3, 4], (3, 4, 5))) is None
    assert source is alias and alias == {3, 4}
    assert source.difference_update(*([3],)) is None and alias == {4}
    assert source.symmetric_difference_update(*([4, 4, 7, 7],)) is None
    assert alias == {7}
    assert source.intersection_update() is None and alias == {7}
    assert source.difference_update() is None and alias == {7}
    source.difference_update(*(source,))
    assert alias == set()
    source.update([8])
    source.symmetric_difference_update(*(source,))
    assert alias == set()
    print("SET_UNPACK_OK")
main()
'''

ORDER = '''events = []
receiver = {0}
def get_receiver():
    events.append(1)
    return receiver
def mark(number, value):
    events.append(number)
    return value
def spread():
    events.append(2)
    yield [1]
    events.append(3)
    yield [2]
    events.append(4)
def fail():
    events.append(6)
    raise ValueError("argument failed")
class HashFailure:
    def __hash__(self):
        events.append(8)
        raise ValueError("hash failed")
def sequence_effect():
    events.append(7)
    return 2
def main():
    result = get_receiver().union(*spread(), mark(5, [3]))
    assert events == [1, 2, 3, 4, 5] and result == {0, 1, 2, 3}
    events.clear()
    try:
        get_receiver().update(*spread(), fail())
    except ValueError as error:
        assert str(error) == "argument failed"
    else:
        raise AssertionError("argument failure swallowed")
    assert events == [1, 2, 3, 4, 6] and receiver == {0}
    events.clear()
    first = HashFailure()
    try:
        set().union(set([first, sequence_effect()]))
    except ValueError as error:
        assert str(error) == "hash failed"
    else:
        raise AssertionError("hash failure swallowed")
    assert events == [7, 8]
    events.clear()
    try:
        set().union(set((first, sequence_effect())))
    except ValueError as error:
        assert str(error) == "hash failed"
    else:
        raise AssertionError("hash failure swallowed")
    assert events == [7, 8]
    print("SET_UNPACK_OK")
main()
'''

ERRORS = '''events = []
def mark(value):
    events.append(value)
    return value
def main():
    source = {1}
    assert source.union(*(), **{}) == {1}
    count = 0
    try:
        source.union(*7)
    except TypeError:
        count += 1
    try:
        source.union(*([2],), ignored=mark(3))
    except TypeError:
        count += 1
    try:
        source.update(*([2],), **{"ignored": mark(4)})
    except TypeError:
        count += 1
    try:
        source.union(**{"ignored": mark(5)}, ignored=mark(6), **{"other": mark(7)})
    except TypeError:
        count += 1
    try:
        source.symmetric_difference(*())
    except TypeError:
        count += 1
    try:
        source.symmetric_difference_update(*([1], [2]))
    except TypeError:
        count += 1
    try:
        source.union(**{1: 2})
    except TypeError:
        count += 1
    assert count == 7 and events == [3, 4, 5, 6]
    assert source == {1}
    print("SET_UNPACK_OK")
main()
'''

ITERATION = '''events = []
def values():
    events.append(1)
    yield 2
    events.append(2)
    yield 9
    events.append(3)
    raise ValueError("iterator failed")
def short():
    events.append(1)
    yield 2
    events.append(2)
    raise ValueError("must not advance")
def empty_source():
    events.append(1)
    yield 8
    events.append(2)
    yield 9
    events.append(3)
def main():
    source = {1, 2, 3}
    try:
        source.update(*(values(),))
    except ValueError as error:
        assert str(error) == "iterator failed"
    else:
        raise AssertionError("update swallowed callback")
    assert source == {1, 2, 3, 9} and events == [1, 2, 3]
    events.clear()
    source = {1, 2, 3}
    try:
        source.difference_update(*(values(),))
    except ValueError:
        pass
    else:
        raise AssertionError("difference swallowed callback")
    assert source == {1, 3} and events == [1, 2, 3]
    events.clear()
    source = {1, 2, 3}
    try:
        source.intersection_update(*(values(),))
    except ValueError:
        pass
    else:
        raise AssertionError("intersection swallowed callback")
    assert source == {1, 2, 3}
    events.clear()
    assert {2}.intersection(*(short(),)) == {2}
    assert events == [1]
    events.clear()
    assert set().intersection(*(empty_source(),)) == set()
    assert events == [1, 2, 3]
    print("SET_UNPACK_OK")
main()
'''

DYNAMIC = '''class Other:
    def union(self, *values, **options):
        return (values, options)
def apply(receiver, args):
    return receiver.union(*args)
def main():
    assert apply({1}, ([2], [3])) == {1, 2, 3}
    assert apply(Other(), (2, 3)) == ((2, 3), {})
    print("SET_UNPACK_OK")
main()
'''

CALLBACKS = '''import gc
hashes = []
class Key:
    def __init__(self, value):
        self.value = value
    def __hash__(self):
        gc.collect()
        hashes.append(self.value)
        return 1
    def __eq__(self, other):
        gc.collect()
        return self.value == other.value
class Bad:
    def __hash__(self):
        gc.collect()
        raise ValueError("hash failed")
def main():
    first = Key(1)
    second = Key(2)
    source = {first}
    other = {second}
    hashes.clear()
    result = source.union(*(other,))
    assert len(result) == 2 and hashes == []
    result = source.intersection(*([Key(1)],))
    assert len(result) == 1
    same = Key(1)
    result = source.intersection(*({same, second},))
    assert next(iter(result)) is first
    result = source.intersection(*({same},))
    assert next(iter(result)) is same
    try:
        source.update(*([second], [Bad()]))
    except ValueError as error:
        assert str(error) == "hash failed"
    else:
        raise AssertionError("hash exception swallowed")
    assert len(source) == 2
    source.difference_update(*([Key(1)],))
    assert len(source) == 1 and second in source
    gc.collect()
    assert len(source) == 1
    print("SET_UNPACK_OK")
main()
'''

FINALIZERS = '''import gc
source = set()
observed = []
class Watch:
    def __del__(self):
        observed.append(2 in source)
        observed.append(len(source))
        source.add(99)
def main():
    token = Watch()
    source.add(token)
    source.add(2)
    del token
    assert source.intersection_update(*([2],)) is None
    gc.collect()
    assert observed == [True, 1]
    assert source == {2, 99}
    print("SET_UNPACK_OK")
main()
'''

OWNERS = '''import gc
import weakref
references = []
finalized = []
class Token:
    def __del__(self):
        finalized.append(1)
def argument():
    value = Token()
    references.append(weakref.ref(value))
    return [value]
def fail():
    gc.collect()
    assert references[0]() is not None
    raise ValueError("later argument")
def main():
    try:
        set().union(*(argument(),), fail())
    except ValueError:
        pass
    else:
        raise AssertionError("later argument failure swallowed")
    gc.collect()
    assert references[0]() is None and finalized == [1]
    result = set().union(*(argument(),))
    gc.collect()
    assert references[1]() is not None and len(result) == 1
    del result
    gc.collect()
    assert references[1]() is None and finalized == [1, 1]
    print("SET_UNPACK_OK")
main()
'''

PROGRAMS = {
    "union": UNION, "methods": METHODS, "order": ORDER, "errors": ERRORS,
    "iteration": ITERATION, "dynamic": DYNAMIC, "callbacks": CALLBACKS,
    "owners": OWNERS, "finalizers": FINALIZERS,
}


@pytest.mark.parametrize("name", PROGRAMS)
def test_set_unpack_reference(name):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(PROGRAMS[name], {})
    assert output.getvalue() == "SET_UNPACK_OK\n"


@pytest.mark.parametrize("name", PROGRAMS)
def test_set_unpack_owned_ir(tmp_path, monkeypatch, name):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / ("set_" + name + ".py")
    output = source.with_suffix(".ll")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on", target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    assert re.search(r"call i64[^\n]*@py_set_call_method_slots\(", text)
    assert "name '*' is not defined" not in text
    assert "name '**' is not defined" not in text
    assert "set.call.result" in text


@pytest.mark.integration
@pytest.mark.parametrize("name", PROGRAMS)
def test_set_unpack_native(tmp_path, pcc_runtime_archive, name):
    source = tmp_path / ("set_" + name + ".py")
    output = source.with_suffix("")
    source.write_text(PROGRAMS[name])
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        environment = dict(os.environ, PATH="", PCC_HOST_PYTHON="/usr/bin/false",
                           PCC_HOST_PCC="/usr/bin/false", PCC_GC_BACKEND=str(gc))
        environment.pop("LC_ALL", None)
        result = subprocess.run([str(output)], env=environment, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == "SET_UNPACK_OK\n" and result.stderr == ""
