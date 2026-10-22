"""Except clauses use the evaluated exception class, including module attrs."""
import os
import subprocess
import sys
import re
import pytest
from pcc.frontends.python.pipeline import compile_python_multi


CELL_HANDLER_SOURCE = '''def outer():
    SelectedError = ValueError
    def invoke(marker=None):
        try:
            raise ValueError("cell")
        except SelectedError:
            return 42
    return invoke()
def main():
    assert outer() == 42
    classes = [ValueError, TypeError]
    try:
        raise TypeError("index")
    except classes[0]:
        raise AssertionError("wrong exception class selected")
    except classes[1]:
        pass
    print("EXCEPTION_CLASS_CELL_AND_INDEX_OK")
main()
'''

EVALUATED_HANDLER_SOURCE = '''def selected():
    return TypeError
def broken():
    raise LookupError("selection")
def main():
    for iteration in range(12):
        try:
            raise TypeError("original")
        except selected():
            pass
        try:
            try:
                raise ValueError("original")
            except [ValueError][1]:
                raise AssertionError("bad index was ignored")
        except IndexError as error:
            assert str(error.__context__) == "original"
        finally:
            print("index")
        try:
            try:
                raise ValueError("original")
            except broken():
                raise AssertionError("selection failure was ignored")
        except LookupError as error:
            assert str(error) == "selection"
            assert str(error.__context__) == "original"
        try:
            try:
                raise ValueError("unmatched")
            except selected():
                raise AssertionError("wrong class matched")
        except ValueError as error:
            assert str(error) == "unmatched"
    print("EVALUATED_HANDLER_OK")
main()
'''

INNER_FINALLY_SOURCE = '''events = []
def mark():
    events.append("inner")
def broken():
    raise LookupError("selection")
def main():
    try:
        try:
            raise ValueError("original")
        except broken():
            raise AssertionError("selection error was ignored")
        finally:
            mark()
    except LookupError as error:
        assert str(error) == "selection"
        assert str(error.__context__) == "original"
    assert events == ["inner"]
    print("EXCEPTION_CLASS_INNER_FINALLY_OK")
main()
'''

SAME_EXCEPTION_SOURCE = '''def raise_again(error):
    raise error
def main():
    original = ValueError("same")
    try:
        try:
            raise original
        except raise_again(original):
            raise AssertionError("selection error was ignored")
    except ValueError as caught:
        assert caught is original
        assert caught.__context__ is None
    print("EXCEPTION_CLASS_SAME_CONTEXT_OK")
main()
'''

NAME_ATTR_TUPLE_SOURCE = '''class Errors:
    Error = ValueError
def selected():
    return TypeError
def main():
    alias = ValueError
    try:
        raise ValueError("name")
    except alias:
        pass
    try:
        raise ValueError("attribute")
    except Errors.Error:
        pass
    try:
        raise TypeError("tuple")
    except (alias, selected()):
        pass
    try:
        try:
            raise ValueError("empty")
        except ():
            raise AssertionError("empty tuple matched")
    except ValueError:
        pass
    print("EXCEPTION_CLASS_NAME_ATTR_TUPLE_OK")
main()
'''

EXPRESSION_PROGRAMS = {
    "cell_index": (CELL_HANDLER_SOURCE, "EXCEPTION_CLASS_CELL_AND_INDEX_OK\n"),
    "evaluated": (EVALUATED_HANDLER_SOURCE, "index\n" * 12 + "EVALUATED_HANDLER_OK\n"),
    "inner_finally": (INNER_FINALLY_SOURCE, "EXCEPTION_CLASS_INNER_FINALLY_OK\n"),
    "same_exception": (SAME_EXCEPTION_SOURCE, "EXCEPTION_CLASS_SAME_CONTEXT_OK\n"),
    "name_attr_tuple": (NAME_ATTR_TUPLE_SOURCE, "EXCEPTION_CLASS_NAME_ATTR_TUPLE_OK\n"),
}


def _expression_ir(source, target=None):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    module = infer_module(parse_and_lift(source, "<exception-class-expression>", "exception_class_expression"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    if target is not None:
        codegen._target_triple = target
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    return text


def _main_blocks(text):
    main = next(body for body in re.findall(r"define [^\n]+\n.*?^\}", text, re.M | re.S)
                if "@user_exception_class_expression_main(" in body.splitlines()[0])
    blocks = {}
    active = None
    for line in main.splitlines()[1:]:
        if line and not line.startswith(" ") and line.endswith(":"):
            active = line[:-1]
            blocks[active] = []
        elif active is not None:
            blocks[active].append(line)
    return blocks


def test_exception_class_evaluation_error_runs_its_own_finally():
    blocks = _main_blocks(_expression_ir(INNER_FINALLY_SOURCE))
    pending = [name for name in blocks if name.startswith("except.class.error")]
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        pending.extend(re.findall(r"label %([\w.$-]+)", "\n".join(blocks[name])))
    runs_finally = any("@user_exception_class_expression_mark(" in "\n".join(blocks[name]) for name in seen)
    assert runs_finally


def test_exception_class_error_does_not_unconditionally_set_self_context():
    blocks = _main_blocks(_expression_ir(SAME_EXCEPTION_SOURCE))
    unguarded_context = [name for name, lines in blocks.items()
                         if name.startswith("except.class.error")
                         and any("@py_exc_set_context(" in line for line in lines)]
    assert not unguarded_context


@pytest.mark.parametrize("name", EXPRESSION_PROGRAMS)
def test_exception_expression_controls_follow_cpython(capsys, name):
    source, expected = EXPRESSION_PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("name", EXPRESSION_PROGRAMS)
@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
def test_exception_expression_programs_reach_owned_objects(name, target):
    from pcc.backend.owned_object_emit import emit_owned_object
    assert len(emit_owned_object(_expression_ir(EXPRESSION_PROGRAMS[name][0], target), target)) > 0


TUPLE_HANDLER_SOURCE = '''events = []
def select(value):
    events.append("select")
    return value
def main():
    for iteration in range(12):
        try:
            raise TypeError("tuple")
        except select((ValueError, TypeError)):
            pass
        try:
            raise ValueError("literal")
        except (select(ValueError), select(TypeError)):
            pass
        try:
            try:
                raise LookupError("empty")
            except ():
                raise AssertionError("empty tuple matched")
        except LookupError as error:
            assert str(error) == "empty"
        try:
            try:
                raise LookupError("dynamic empty")
            except select(()):
                raise AssertionError("dynamic empty tuple matched")
        except LookupError as error:
            assert str(error) == "dynamic empty"
    assert len(events) == 48
    print("TUPLE_HANDLER_OK")
main()
'''

INVALID_HANDLER_SOURCE = '''class Plain:
    pass
def select(value):
    return value
def main():
    invalid = [42, None, Plain, ValueError("instance"), [ValueError],
               (ValueError, 42), (42, ValueError), ((ValueError,),),
               (ValueError, (TypeError,))]
    for handler in invalid:
        for matched in [True, False]:
            try:
                try:
                    if matched:
                        raise ValueError("original")
                    raise LookupError("original")
                except select(handler):
                    raise AssertionError("invalid handler accepted")
            except TypeError as error:
                assert str(error) == "catching classes that do not inherit from BaseException is not allowed"
                assert str(error.__context__) == "original"
    try:
        try:
            raise ValueError("literal")
        except (ValueError, 42):
            raise AssertionError("invalid literal handler accepted")
    except TypeError as error:
        assert str(error.__context__) == "literal"
    print("INVALID_HANDLER_OK")
main()
'''

SAME_EXCEPTION_HANDLER_SOURCE = '''def reraiser(error):
    raise error
def main():
    for iteration in range(12):
        original = ValueError("same")
        try:
            try:
                raise original
            except reraiser(original):
                raise AssertionError("reraising selector matched")
        except ValueError as error:
            assert error is original
            assert error.__context__ is None
    print("SAME_EXCEPTION_HANDLER_OK")
main()
'''

GENERATOR_HANDLER_SOURCE = '''import gc
events = []
def choosing():
    try:
        raise ValueError("parked original")
    except (yield "choose"):
        yield "caught"
    finally:
        events.append("finally")
def main():
    for iteration in range(3):
        iterator = choosing()
        assert next(iterator) == "choose"
        gc.collect()
        assert iterator.send(ValueError) == "caught"
        iterator.close()
        iterator = choosing()
        assert next(iterator) == "choose"
        gc.collect()
        try:
            iterator.send(TypeError)
            raise AssertionError("unmatched handler accepted")
        except ValueError as error:
            assert str(error) == "parked original"
            assert error.__context__ is None
        iterator = choosing()
        assert next(iterator) == "choose"
        gc.collect()
        try:
            iterator.send(42)
            raise AssertionError("invalid handler accepted")
        except TypeError as error:
            assert str(error.__context__) == "parked original"
        iterator = choosing()
        assert next(iterator) == "choose"
        gc.collect()
        try:
            iterator.throw(LookupError("selection"))
            raise AssertionError("throw was ignored")
        except LookupError as error:
            assert str(error) == "selection"
            assert str(error.__context__) == "parked original"
        iterator = choosing()
        assert next(iterator) == "choose"
        gc.collect()
        iterator.close()
    assert len(events) == 15
    print("GENERATOR_HANDLER_OK")
main()
'''


MODULE_HANDLER_SOURCE = '''import gc
def selected():
    gc.collect()
    return (ValueError, TypeError)
try:
    raise ValueError("module original")
except selected() as error:
    assert str(error) == "module original"
try:
    try:
        raise ValueError("module invalid")
    except (ValueError, 42):
        raise AssertionError("invalid module handler accepted")
except TypeError as error:
    assert str(error.__context__) == "module invalid"
print("MODULE_HANDLER_OK")
'''


MULTI_YIELD_HANDLER_SOURCE = '''import gc

events = []

def choosing():
    try:
        raise ValueError("parked original")
    except ((yield "first"), (yield "second")):
        yield "caught"
    finally:
        events.append("finally")

def tuple_values():
    result = ((yield "first"), *(yield "spread"), (yield "last"))
    yield result

def main():
    for iteration in range(3):
        iterator = choosing()
        assert next(iterator) == "first"
        gc.collect()
        assert iterator.send(TypeError) == "second"
        gc.collect()
        assert iterator.send(ValueError) == "caught"
        gc.collect()
        iterator.close()
        iterator = choosing()
        assert next(iterator) == "first"
        gc.collect()
        assert iterator.send(ValueError) == "second"
        gc.collect()
        try:
            iterator.throw(LookupError("selection"))
            raise AssertionError("throw ignored")
        except LookupError as error:
            assert str(error.__context__) == "parked original"
        iterator = choosing()
        assert next(iterator) == "first"
        gc.collect()
        iterator.close()
        iterator = choosing()
        assert next(iterator) == "first"
        assert iterator.send(ValueError) == "second"
        gc.collect()
        iterator.close()
        iterator = choosing()
        assert next(iterator) == "first"
        assert iterator.send(ValueError) == "second"
        gc.collect()
        try:
            iterator.send(42)
            raise AssertionError("invalid later member ignored")
        except TypeError as error:
            assert str(error.__context__) == "parked original"
        iterator = tuple_values()
        assert next(iterator) == "first"
        assert iterator.send(["kept"]) == "spread"
        gc.collect()
        assert iterator.send(["expanded", "in order"]) == "last"
        gc.collect()
        result = iterator.send(["last"])
        assert result[0][0] == "kept"
        assert result[1] == "expanded"
        assert result[2] == "in order"
        assert result[3][0] == "last"
        iterator.close()
    assert len(events) == 15
    print("MULTI_YIELD_HANDLER_OK")

main()
'''


DYNAMIC_GENERATOR_CALL_SOURCE = '''import gc
def callback():
    return 42
def gen(f):
    yield f()
def main():
    for iteration in range(3):
        iterator = gen(callback)
        assert next(iterator) == 42
        gc.collect()
        iterator.close()
    print("DYNAMIC_GENERATOR_CALL_OK")
main()
'''


HANDLED_SCOPE_SOURCE = '''import gc

events = []

def reraiser():
    raise

def replacement():
    raise TypeError("replacement")

def handled_message():
    try:
        reraiser()
    except BaseException as error:
        return str(error)

def parked(label):
    try:
        raise ValueError(label)
    except ValueError:
        try:
            yield label
            reraiser()
        finally:
            events.append(handled_message())

finalizer_events = []

class Watch:
    def __del__(self):
        finalizer_events.append(handled_message())

def no_context():
    try:
        reraiser()
    except RuntimeError:
        return True
    except BaseException:
        return False

def returning():
    try:
        raise ValueError("returning")
    except ValueError:
        return 42

def return_and_park():
    try:
        try:
            raise ValueError("retired")
        except ValueError:
            return 42
    finally:
        assert no_context()
        yield "cleanup"
        assert no_context()


def main():
    assert returning() == 42
    assert no_context()
    for index in range(3):
        try:
            raise ValueError("loop")
        except ValueError:
            if index == 0:
                continue
            break
    assert no_context()
    iterator = return_and_park()
    assert next(iterator) == "cleanup"
    gc.collect()
    assert no_context()
    try:
        next(iterator)
        raise AssertionError("generator return did not complete")
    except StopIteration as error:
        assert error.value == 42
    assert no_context()
    try:
        raise ValueError("finalizer")
    except ValueError:
        watched = Watch()
        del watched
        gc.collect()
        assert finalizer_events == ["finalizer"]
    assert no_context()
    original = ValueError("selector")
    try:
        try:
            raise original
        except reraiser():
            raise AssertionError("selector returned")
    except ValueError as error:
        assert error is original
        assert error.__context__ is None
    original = ValueError("outer")
    try:
        raise original
    except ValueError:
        assert handled_message() == "outer"
        try:
            raise LookupError("inner")
        except LookupError:
            assert handled_message() == "inner"
        assert handled_message() == "outer"
        try:
            replacement()
        except TypeError as error:
            assert error.__context__ is original
        assert handled_message() == "outer"
    try:
        reraiser()
        raise AssertionError("handled state leaked")
    except RuntimeError:
        pass
    original = ValueError("finally")
    try:
        try:
            raise original
        finally:
            assert handled_message() == "finally"
            reraiser()
    except ValueError as error:
        assert error is original
        assert error.__context__ is None
    first = parked("first")
    second = parked("second")
    assert next(first) == "first"
    assert next(second) == "second"
    gc.collect()
    try:
        raise LookupError("caller")
    except LookupError:
        assert handled_message() == "caller"
        try:
            next(first)
            raise AssertionError("generator did not reraise")
        except ValueError as error:
            assert str(error) == "first"
        assert handled_message() == "caller"
        assert next(parked("temporary")) == "temporary"
        assert handled_message() == "caller"
    gc.collect()
    try:
        second.throw(LookupError("injected"))
        raise AssertionError("throw did not propagate")
    except LookupError as error:
        assert str(error) == "injected"
        assert str(error.__context__) == "second"
    closing = parked("closing")
    assert next(closing) == "closing"
    gc.collect()
    closing.close()
    assert events[0] == "first"
    assert "injected" in events
    assert "" in events
    try:
        reraiser()
        raise AssertionError("generator handled state leaked")
    except RuntimeError:
        pass
    print("HANDLED_EXCEPTION_SCOPES_OK")

main()
'''


RETURN_CLEANUP_SOURCE = '''import gc

events = []

def reraiser():
    raise

def no_context():
    try:
        reraiser()
    except RuntimeError:
        return True
    except BaseException:
        return False

def capture():
    try:
        raise ValueError("kept")
    except ValueError as error:
        return error

def overriding_return():
    try:
        raise ValueError("retired")
    except ValueError:
        try:
            return 1
        finally:
            return 2

def overriding_break():
    for index in range(2):
        try:
            raise ValueError("retired break")
        except ValueError:
            try:
                return 1
            finally:
                break
    assert no_context()
    return 2

def overriding_continue():
    for index in range(2):
        try:
            raise ValueError("retired continue")
        except ValueError:
            try:
                return 1
            finally:
                continue
    assert no_context()
    return 3

def rebind():
    original = ["original"]
    try:
        return original
    finally:
        original = ["replacement"]
        gc.collect()

def delete():
    original = ["original"]
    try:
        return original
    finally:
        del original
        gc.collect()

def nested_finally():
    try:
        try:
            raise ValueError("nested")
        except ValueError:
            try:
                return 1
            finally:
                events.append("inner")
                return 2
    finally:
        events.append("outer")

def generator_capture():
    try:
        raise ValueError("generator kept")
    except ValueError as error:
        try:
            return error
        finally:
            yield "cleanup"
            gc.collect()

def main():
    assert str(capture()) == "kept"
    assert no_context()
    assert overriding_return() == 2
    assert no_context()
    assert overriding_break() == 2
    assert no_context()
    assert overriding_continue() == 3
    assert no_context()
    assert rebind() == ["original"]
    assert delete() == ["original"]
    assert nested_finally() == 2
    assert events == ["inner", "outer"]
    assert no_context()
    iterator = generator_capture()
    assert next(iterator) == "cleanup"
    gc.collect()
    try:
        next(iterator)
        raise AssertionError("generator failed to return")
    except StopIteration as stopped:
        assert str(stopped.value) == "generator kept"
    assert no_context()
    print("RETURN_CLEANUP_SCOPES_OK")

main()
'''


IMPLICIT_CHAINING_SOURCE = '''import gc

def helper(error):
    raise error

def caused(error, cause):
    raise error from cause

def main():
    reused = TypeError("reused")
    first = ValueError("first")
    try:
        raise first
    except ValueError:
        try:
            raise reused
        except TypeError as error:
            assert error.__context__ is first
    second = LookupError("second")
    try:
        raise second
    except LookupError:
        try:
            helper(reused)
        except TypeError as error:
            assert error is reused
            assert error.__context__ is second
    self_error = ValueError("self")
    try:
        try:
            raise self_error
        except ValueError:
            helper(self_error)
    except ValueError as error:
        assert error is self_error
        assert error.__context__ is None
    a = ValueError("A")
    b = TypeError("B")
    try:
        try:
            raise a
        except ValueError:
            try:
                raise b
            except TypeError:
                helper(a)
    except ValueError as error:
        assert error is a
        assert a.__context__ is b
        assert b.__context__ is None
    cause = LookupError("explicit")
    target = TypeError("target")
    try:
        raise first
    except ValueError:
        try:
            caused(target, cause)
        except TypeError as error:
            assert error.__context__ is first
            assert error.__cause__ is cause
            assert error.__suppress_context__
    try:
        raise second
    except LookupError:
        try:
            helper(target)
        except TypeError as error:
            assert error.__context__ is second
            assert error.__cause__ is cause
            assert error.__suppress_context__
    try:
        raise first
    except ValueError:
        try:
            caused(target, None)
        except TypeError as error:
            assert error.__context__ is first
            assert error.__cause__ is None
            assert error.__suppress_context__
    gc.collect()
    print("IMPLICIT_CHAINING_OK")

main()
'''


FINALLY_CLEANUP_ONCE_SOURCE = '''import gc

events = []
released = []

class Payload:
    def __init__(self, label):
        self.label = label
    def __del__(self):
        released.append(self.label)

def cleanup(label):
    events.append(label)
    raise ValueError(label)

def returning():
    try:
        return Payload("returned")
    finally:
        cleanup("return")

def normal():
    try:
        pass
    finally:
        cleanup("normal")

def generator():
    try:
        return Payload("generator")
    finally:
        yield "park"
        cleanup("generator")

def breaking():
    for index in range(1):
        try:
            return Payload("break")
        finally:
            break
    gc.collect()
    assert "break" in released

def continuing():
    for index in range(2):
        try:
            return Payload("continue")
        finally:
            continue
    gc.collect()
    assert released.count("continue") == 2

def else_failure():
    try:
        try:
            pass
        except ValueError:
            events.append("wrong handler")
        else:
            raise ValueError("else")
    except ValueError as error:
        assert str(error) == "else"

def local_loop():
    try:
        return Payload("local-loop")
    finally:
        for index in range(3):
            if index == 0:
                continue
            break
        gc.collect()
        assert "local-loop" not in released

def local_generator_loop():
    try:
        return Payload("local-generator-loop")
    finally:
        for index in range(3):
            yield index
            if index == 0:
                continue
            break
        gc.collect()
        assert "local-generator-loop" not in released

def caught_inner_return():
    try:
        return Payload("outer-return")
    finally:
        try:
            try:
                return Payload("inner-return")
            finally:
                raise ValueError("caught cleanup")
        except ValueError:
            pass
        gc.collect()
        assert "inner-return" in released
        assert "outer-return" not in released

class Manager:
    def __enter__(self):
        return self
    def __exit__(self, kind, error, trace):
        events.append("exit")
        raise ValueError("exit")

def with_failure():
    try:
        with Manager():
            return Payload("with-return")
    except ValueError:
        gc.collect()
        assert "with-return" in released

def caught_with_return():
    try:
        return Payload("outer-with-return")
    finally:
        try:
            with Manager():
                return Payload("inner-with-return")
        except ValueError:
            pass
        gc.collect()
        assert "inner-with-return" in released
        assert "outer-with-return" not in released

def canceled_generator_return():
    for index in range(1):
        try:
            return Payload("canceled-generator-return")
        finally:
            yield "park"
            break
    gc.collect()
    assert "canceled-generator-return" in released
    yield "done"

class FinalizedError(Exception):
    def __del__(self):
        released.append("handler-error")

def retired_generator_handler():
    try:
        raise FinalizedError("retired")
    except FinalizedError:
        yield "park"
    gc.collect()
    assert "handler-error" in released
    yield "done"

def main():
    try:
        returning()
    except ValueError as error:
        assert str(error) == "return"
    gc.collect()
    assert released == ["returned"]
    try:
        normal()
    except ValueError as error:
        assert str(error) == "normal"
    iterator = generator()
    assert next(iterator) == "park"
    gc.collect()
    try:
        next(iterator)
    except ValueError as error:
        assert str(error) == "generator"
    gc.collect()
    assert released == ["returned", "generator"]
    breaking()
    continuing()
    else_failure()
    result = local_loop()
    assert result.label == "local-loop"
    iterator = local_generator_loop()
    assert next(iterator) == 0
    assert next(iterator) == 1
    try:
        next(iterator)
    except StopIteration as error:
        assert error.value.label == "local-generator-loop"
    result = caught_inner_return()
    assert result.label == "outer-return"
    with_failure()
    result = caught_with_return()
    assert result.label == "outer-with-return"
    iterator = canceled_generator_return()
    assert next(iterator) == "park"
    gc.collect()
    assert next(iterator) == "done"
    iterator.close()
    iterator = retired_generator_handler()
    assert next(iterator) == "park"
    gc.collect()
    assert next(iterator) == "done"
    iterator.close()
    assert events == ["return", "normal", "generator", "exit", "exit"]
    print("FINALLY_CLEANUP_ONCE_OK")

main()
'''


SUPPRESSION_SOURCE = '''import gc
import traceback

class TruthTrap:
    def __bool__(self):
        raise AssertionError("suppression coerced a non-bool")

def main():
    error = TypeError("flag")
    assert error.__suppress_context__ is False
    error.__suppress_context__ = True
    gc.collect()
    assert error.__suppress_context__ is True
    for invalid in [0, 1, None, "", [], TruthTrap()]:
        try:
            error.__suppress_context__ = invalid
        except TypeError as failure:
            assert str(failure) == "attribute value type must be bool"
        else:
            raise AssertionError("non-bool suppression accepted")
        assert error.__suppress_context__ is True
    try:
        del error.__suppress_context__
    except TypeError as failure:
        assert str(failure) == "can't delete numeric/char attribute"
    else:
        raise AssertionError("suppression deletion accepted")
    error.__suppress_context__ = False
    assert error.__suppress_context__ is False
    try:
        try:
            raise ValueError("hidden-context")
        except ValueError:
            raise TypeError("shown-target") from None
    except TypeError as failure:
        assert failure.__suppress_context__ is True
        assert str(failure.__context__) == "hidden-context"
        text = traceback.format_exc()
        assert "TypeError: shown-target" in text
        assert "ValueError: hidden-context" not in text
        failure.__suppress_context__ = False
        text = traceback.format_exc()
        assert "ValueError: hidden-context" in text
        assert "During handling of the above exception" in text
    try:
        try:
            raise ValueError("unshown-context")
        except ValueError:
            raise TypeError("caused-target") from LookupError("shown-cause")
    except TypeError as failure:
        assert failure.__suppress_context__ is True
        failure.__suppress_context__ = False
        text = traceback.format_exc()
        assert "LookupError: shown-cause" in text
        assert "ValueError: unshown-context" not in text
        assert "The above exception was the direct cause" in text
    print("SUPPRESSION_OK")

main()
'''

GENERATOR_CLOSE_SOURCE = '''import gc

events = []

class Payload:
    def __str__(self):
        gc.collect()
        return "payload-str"
    def __repr__(self):
        gc.collect()
        return "payload-repr"

def catching():
    try:
        yield "ready"
    except GeneratorExit as error:
        assert str(error) == ""
        assert error.args == ()
        events.append("caught")

def escaping():
    try:
        yield "ready"
    except GeneratorExit:
        raise BaseException("escape")

def replacement():
    try:
        yield "ready"
    except GeneratorExit:
        raise GeneratorExit("replacement")

def ignoring():
    try:
        yield "ready"
    except GeneratorExit:
        yield "ignored"
        yield "after"

def returning(value):
    try:
        yield "ready"
    except GeneratorExit:
        return value

def main():
    for cls in [ValueError, TypeError, GeneratorExit]:
        empty = cls()
        assert empty.args == ()
        assert str(empty) == ""
        assert repr(empty) == cls.__name__ + "()"
        explicit_none = cls(None)
        assert explicit_none.args == (None,)
        assert str(explicit_none) == "None"
        assert repr(explicit_none) == cls.__name__ + "(None)"
        empty_text = cls("")
        assert empty_text.args == ("",)
        assert str(empty_text) == ""
        assert repr(empty_text) == cls.__name__ + "('')"
    payload = Payload()
    error = ValueError(payload)
    gc.collect()
    assert error.args[0] is payload
    assert str(error) == "payload-str"
    assert repr(error) == "ValueError(payload-repr)"
    try:
        raise GeneratorExit()
    except GeneratorExit as error:
        assert error.args == ()
    iterator = catching()
    assert next(iterator) == "ready"
    assert iterator.close() is None
    assert events == ["caught"]
    iterator = escaping()
    assert next(iterator) == "ready"
    try:
        iterator.close()
    except BaseException as error:
        assert type(error) is BaseException
        assert str(error) == "escape"
    else:
        raise AssertionError("unrelated BaseException swallowed")
    iterator = replacement()
    assert next(iterator) == "ready"
    assert iterator.close() is None
    iterator = ignoring()
    assert next(iterator) == "ready"
    try:
        iterator.close()
    except RuntimeError as error:
        assert str(error) == "generator ignored GeneratorExit"
    else:
        raise AssertionError("ignored close accepted")
    assert next(iterator) == "after"
    assert iterator.close() is None
    iterator = returning(payload)
    assert next(iterator) == "ready"
    result = iterator.close()
    gc.collect()
    assert result is payload
    assert iterator.close() is None
    never_started = catching()
    assert never_started.close() is None
    assert events == ["caught"]
    print("GENERATOR_CLOSE_OK")

main()
'''


HANDLER_PROGRAMS = [
    pytest.param(SUPPRESSION_SOURCE, "SUPPRESSION_OK\n", id="suppression"),
    pytest.param(GENERATOR_CLOSE_SOURCE, "GENERATOR_CLOSE_OK\n", id="generator-close"),
    pytest.param(FINALLY_CLEANUP_ONCE_SOURCE, "FINALLY_CLEANUP_ONCE_OK\n", id="finally-cleanup-once"),
    pytest.param(IMPLICIT_CHAINING_SOURCE, "IMPLICIT_CHAINING_OK\n", id="implicit-chaining"),
    pytest.param(RETURN_CLEANUP_SOURCE, "RETURN_CLEANUP_SCOPES_OK\n", id="return-cleanup-scopes"),
    pytest.param(HANDLED_SCOPE_SOURCE, "HANDLED_EXCEPTION_SCOPES_OK\n", id="handled-scopes"),
    pytest.param(DYNAMIC_GENERATOR_CALL_SOURCE, "DYNAMIC_GENERATOR_CALL_OK\n", id="dynamic-generator-call"),
    pytest.param(MULTI_YIELD_HANDLER_SOURCE, "MULTI_YIELD_HANDLER_OK\n", id="multi-yield-handler"),
    pytest.param(MODULE_HANDLER_SOURCE, "MODULE_HANDLER_OK\n", id="module-handler"),
    pytest.param(CELL_HANDLER_SOURCE, "EXCEPTION_CLASS_CELL_AND_INDEX_OK\n", id="cell-index"),
    pytest.param(EVALUATED_HANDLER_SOURCE, "index\n" * 12 + "EVALUATED_HANDLER_OK\n", id="call-selection-error"),
    pytest.param(TUPLE_HANDLER_SOURCE, "TUPLE_HANDLER_OK\n", id="tuple-empty-dynamic"),
    pytest.param(INVALID_HANDLER_SOURCE, "INVALID_HANDLER_OK\n", id="invalid-handler"),
    pytest.param(SAME_EXCEPTION_HANDLER_SOURCE, "SAME_EXCEPTION_HANDLER_OK\n", id="same-exception"),
    pytest.param(GENERATOR_HANDLER_SOURCE, "GENERATOR_HANDLER_OK\n", id="yielding-handler"),
]


@pytest.mark.parametrize("target", ("arm64-apple-darwin", "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"))
@pytest.mark.parametrize("program, expected", HANDLER_PROGRAMS)
def test_exception_class_cell_and_index_reach_owned_objects(target, program, expected):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module

    module = infer_module(parse_and_lift(program, "<exception-class-cell>", "exception_class_cell"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._target_triple = target
    codegen._strict_no_libpython = True
    text = codegen.generate(module)
    has_unavailable_function = "strict.nolib.stub:" in text
    assert not has_unavailable_function
    assert len(emit_owned_object(text, target)) > 0


@pytest.mark.integration
@pytest.mark.parametrize("program, expected", HANDLER_PROGRAMS)
def test_exception_class_cell_and_index_execute_all_collectors(tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, program, expected):
    source = tmp_path / "exception_class_cell.py"
    source.write_text(program)
    # Python 3.15 warns about the deliberately tested overriding exits; their
    # still-supported return/break/continue semantics remain the oracle.
    oracle_args = [sys.executable]
    if program in (RETURN_CLEANUP_SOURCE, FINALLY_CLEANUP_ONCE_SOURCE):
        oracle_args.extend(["-W", "ignore::SyntaxWarning"])
    oracle = subprocess.run(oracle_args + [str(source)], capture_output=True, text=True, timeout=20)
    assert (oracle.returncode, oracle.stdout, oracle.stderr) == (0, expected, "")
    binary = tmp_path / "exception_class_cell"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20, env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert result.returncode == 0 and result.stdout == expected and result.stderr == "", (backend, result.returncode, result.stdout, result.stderr)


def test_module_and_local_exception_class_selection(tmp_path, pcc_runtime_archive):
    errors = tmp_path / 'error_types.py'
    errors.write_text('''class First(Exception):
    pass
class Second(Exception):
    pass
class FailingConstructor:
    def __init__(self):
        raise First("constructor")
def trigger(first):
    if first:
        raise First("first")
    raise Second("second")
''', encoding='utf-8')
    source = tmp_path / 'error_use.py'
    source.write_text('''import error_types
def main():
    for first in [True, False]:
        try:
            error_types.trigger(first)
        except error_types.First:
            print("first")
        except error_types.Second:
            print("second")
    selected = error_types.Second
    try:
        error_types.trigger(False)
    except selected:
        print("selected")
    try:
        error_types.FailingConstructor()
    except error_types.First:
        print("constructor")
main()
''', encoding='utf-8')
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / 'error_use'
    compile_python_multi([str(errors), str(source)], str(binary),
                         module_names=['error_types', 'error_use'], entry_module='error_use',
                         backend='self', libpython_mode='off', runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f'GC{gc}: {result.stderr}'
        assert result.stdout == expected.stdout, f'GC{gc}: {result.stdout}'


HANDLED_THREAD_SOURCE = '''from threading import Thread
import gc

results = []

def reraiser():
    raise

def handled_message():
    try:
        reraiser()
    except BaseException as error:
        return str(error)

def worker(label):
    try:
        reraiser()
    except RuntimeError:
        results.append(True)
    else:
        results.append(False)
    for iteration in range(10):
        try:
            raise ValueError(label)
        except ValueError:
            gc.collect()
            results.append(handled_message() == label)

def main():
    first = Thread(target=worker, args=("first",))
    second = Thread(target=worker, args=("second",))
    try:
        raise LookupError("main")
    except LookupError:
        first.start()
        second.start()
        first.join()
        second.join()
        assert handled_message() == "main"
    assert len(results) == 22
    for result in results:
        assert result
    print("HANDLED_THREADS_OK")

main()
'''


HANDLED_VTHREAD_SOURCE = '''import pcc.virtual_thread as vt

def reraiser():
    raise

def handled_message():
    try:
        reraiser()
    except BaseException as error:
        return str(error)

def child(label):
    try:
        reraiser()
    except RuntimeError:
        pass
    else:
        raise AssertionError("carrier handled state leaked into task")
    try:
        raise ValueError(label)
    except ValueError:
        assert handled_message() == label
        vt.yield_now()
        assert handled_message() == label
        vt.yield_now()
        assert handled_message() == label
    return label

def selecting():
    vt.yield_now()
    reraiser()

def selection_task():
    original = ValueError("selection")
    try:
        try:
            raise original
        except selecting():
            raise AssertionError("selector returned")
    except ValueError as error:
        assert error is original
        assert error.__context__ is None
    return "selection"

def main():
    left = vt.spawn(child, "left")
    right = vt.spawn(child, "right")
    selected = vt.spawn(selection_task)
    try:
        raise LookupError("carrier")
    except LookupError:
        assert handled_message() == "carrier"
        vt.run(1, 256)
        assert handled_message() == "carrier"
    assert vt.result(left) == "left"
    assert vt.result(right) == "right"
    assert vt.result(selected) == "selection"
    print("HANDLED_VTHREAD_OK")

main()
'''


@pytest.mark.integration
def test_handled_exception_thread_isolation(tmp_path, monkeypatch, threaded_pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "handled_threads.py"
    source.write_text(HANDLED_THREAD_SOURCE)
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=30)
    assert (oracle.returncode, oracle.stdout, oracle.stderr) == (0, "HANDLED_THREADS_OK\n", "")
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    binary = tmp_path / "handled_threads"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=40,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert (result.returncode, result.stdout, result.stderr) == (0, "HANDLED_THREADS_OK\n", ""), (backend, result)


@pytest.mark.integration
def test_handled_exception_virtual_thread_isolation(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "handled_vthread.py"
    source.write_text(HANDLED_VTHREAD_SOURCE)
    binary = tmp_path / "handled_vthread"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert (result.returncode, result.stdout, result.stderr) == (0, "HANDLED_VTHREAD_OK\n", ""), (backend, result)


@pytest.mark.integration
@pytest.mark.parametrize("mode", ("suppressed", "restored", "cause"))
def test_exception_suppression_unhandled_traceback(tmp_path, pcc_runtime_archive, python_program_compiler, mode):
    source = tmp_path / "suppression_trace.py"
    cause = 'LookupError("explicit-cause")' if mode == "cause" else "None"
    toggle = "error.__suppress_context__ = False" if mode == "restored" else "pass"
    source.write_text('''def main():
    try:
        try:
            raise ValueError("implicit-context")
        except ValueError:
            raise TypeError("visible-target") from ''' + cause + '''
    except TypeError as error:
        ''' + toggle + '''
        raise
main()
''')
    oracle = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 1 and oracle.stdout == ""
    def chain_lines(stderr):
        return [line for line in stderr.splitlines() if line.startswith((
            "ValueError:", "TypeError:", "LookupError:",
            "During handling of the above exception", "The above exception was the direct cause",
        ))]
    expected = chain_lines(oracle.stderr)
    binary = tmp_path / "suppression_trace"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend), PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert result.returncode == 1 and result.stdout == "", (backend, result)
        assert chain_lines(result.stderr) == expected, (backend, result.stderr, oracle.stderr)
        assert str(source) in result.stderr
        assert 'raise TypeError("visible-target") from ' + cause in result.stderr
