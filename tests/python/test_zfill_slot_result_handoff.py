"""Temporary string padding publishes before retiring its receiver/width roots."""
import pytest
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication

@pytest.mark.parametrize("expression", (
    "'prefix' + str(abs(value)).zfill(2)",
    "take(str(value).zfill(width()))",
    "str(value).zfill(width())",
))
def test_temporary_zfill_result_is_published_before_operand_cleanup(expression):
    text = _emit("def width():\n    return 3\ndef take(value):\n    return value\ndef probe(value):\n    return " + expression + "\n")
    _assert_immediate_publication(text, "py_str_zfill")
    assert "@py_index_i64_checked_slots(" in text
    if "strict.nolib.stub" in text:
        pytest.fail("unexpected no-libpython stub")


def test_tuple_store_compiles_to_catchable_native_assignment_protocol():
    text = _emit("def probe():\n    values=(1,2)\n    try:\n        values[0]=3\n    except TypeError:\n        return 'tuple'\n    return 'wrong'\n")
    assert "@py_obj_assign_subscript(" in text
    if "strict.nolib.stub" in text:
        pytest.fail("unexpected no-libpython stub")


def test_module_enumerate_comprehension_unpacks_into_registered_private_roots():
    text = _emit("names={'a':1,'b':2}\ncodes={name:index+1 for index,name in enumerate(names)}\nprint(codes)\n")
    assert "@pcc_gc_root_move(" in text
    assert "@pcc_gc_frame_enter(" in text


PROGRAM = "import gc\nevents=[]\nclass Width:\n    def __index__(self):\n        events.append('index')\n        gc.collect()\n        return 6\ndef receiver():\n    events.append('receiver')\n    return str(-42)\ndef argument():\n    events.append('argument')\n    return Width()\ndef pad():\n    return '['+receiver().zfill(argument())+']'\nprint(pad())\nprint(events)\nevents=[]\ndef rhs():\n    events.append('rhs')\n    return str(9)\ndef target():\n    events.append('target')\n    return (1,2)\ndef key():\n    events.append('key')\n    return 0\ntry:\n    try:\n        target()[key()]=rhs()\n    finally:\n        events.append('finally')\nexcept TypeError:\n    events.append('type')\nprint(events)\n\ncodes={name:index+1 for index,name in enumerate({'first':1,'second':2})}\nprint(codes)\ntry:\n    index\nexcept NameError:\n    print('private-index')\n"


@pytest.mark.integration
def test_runtime_compiler_patterns_execute_self_backend(tmp_path):
    import subprocess
    import sys
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "runtime_patterns.py"
    source.write_text(PROGRAM)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10, check=True)
    executable = tmp_path / "runtime_patterns"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off", ir_scaffold_mode="on")
    actual = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert (actual.returncode, actual.stdout, actual.stderr) == (0, expected.stdout, expected.stderr)


@pytest.mark.parametrize("prefix,call", (
    ("import sys\n", "sys.setrecursionlimit(limit)"),
    ("from sys import setrecursionlimit as change\n", "change(limit)"),
))
def test_public_recursion_limit_setter_uses_owned_runtime(prefix,call):
    text = _emit(prefix + "def probe(limit):\n    " + call + "\n    return None\n")
    assert "@py_sys_setrecursionlimit(" in text
    assert "@py_index_i64_checked_slots(" in text
    if "strict.nolib.stub" in text:
        pytest.fail("unexpected no-libpython stub")
    body = _function(text)
    if "@py_cpy_ensure_init(" in body:
        pytest.fail("unexpected CPython initialization call in source function")


SETTER_PROGRAM = "import sys\nfrom sys import setrecursionlimit as change\nold=sys.getrecursionlimit()\nclass Limit:\n    def __index__(self):\n        return old+100\nprint(change(Limit()) is None)\nprint(sys.getrecursionlimit()==old+100)\nsys.setrecursionlimit(old)\nfor value in [0,-1,2147483648,1<<80]:\n    try:\n        change(value)\n    except ValueError:\n        print('value')\n    except OverflowError:\n        print('overflow')\ntry:\n    change(1.5)\nexcept TypeError:\n    print('type')\nprint(sys.getrecursionlimit()==old)\n"


@pytest.mark.integration
def test_public_recursion_setter_executes_self_backend(tmp_path):
    import subprocess
    import sys
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "recursion_setter.py"
    source.write_text(SETTER_PROGRAM)
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=10, check=True)
    executable = tmp_path / "recursion_setter"
    compile_python(str(source), str(executable), backend="self", libpython_mode="off", ir_scaffold_mode="on")
    actual = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert (actual.returncode, actual.stdout, actual.stderr) == (0, expected.stdout, expected.stderr)
