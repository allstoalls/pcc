"""The explicit throw-argument edge precedes handled-exception restoration."""
import re
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_verify import verify_parsed_module
from tests.python.test_slot_call_operand_roots import _emit


def test_await_throw_arguments_have_a_separate_owned_resume_edge():
    text = _emit('''async def child():
    return 7
async def probe():
    try:
        return slot_operand_probe(await child())
    except ValueError:
        return 19
''')
    verify_parsed_module(parse_self_backend_module(text))
    body = re.search(r'^define [^\n]*@user_slot_operand_probe__gen_resume\([^\n]*\).*?^}',text,re.M|re.S).group(0)
    assert '@py_coroutine_has_throw_arguments(' in body
    assert '@py_coroutine_take_throw_arguments(' in body
    assert '@py_await_throw_arguments(' in body
    block = re.search(r'^await\.throw_arguments[^\n]*:\n(.*?)(?=^[^ \n][^\n]*:\n|^})',body,re.M|re.S)
    assert block is not None
    block=block.group(1)
    take=re.search(r'(%[^ ]+) = call ptr[^\n]*@py_coroutine_take_throw_arguments[^\n]*\n([^\n]*)',block)
    assert take is not None
    assert 'store ptr ' + take.group(1) in take.group(2)
    assert '@py_tls_exc_swap_slot(' not in block[:take.start()]
    assert re.search(r'call i64[^\n]*@py_coroutine_has_throw_arguments[^\n]*\n[^\n]*\n  br i1[^\n]*label %await\.throw_arguments', body)
    assert 'await.throw_args' in body


def test_entire_native_protocol_oracle_emits_valid_owned_ir():
    import ast
    from pathlib import Path
    tree=ast.parse(Path(__file__).with_name('test_native_coroutine_protocol.py').read_text())
    program=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and node.targets[0].id == '_PROGRAM')
    text=_emit(program)
    verify_parsed_module(parse_self_backend_module(text))
    assert '@py_await_throw_arguments(' in text


def test_coroutine_protocol_exports_match_runtime_abi_and_public_header():
    import ast
    from pathlib import Path
    from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES
    root=Path(__file__).parents[2]
    runtime=ast.parse((root/'pcc/runtime/py/py_coroutine.py').read_text())
    exported={ast.literal_eval(decorator.args[0]): node for node in runtime.body if isinstance(node,ast.FunctionDef) for decorator in node.decorator_list if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Name) and decorator.func.id == 'c_abi_export'}
    header=(root/'pcc/runtime/include/py_runtime.h').read_text()
    for name,result,args,prototype in (
        ('py_coroutine_bound_method','ptr',['ptr','i64'],'PyObject *py_coroutine_bound_method(PyObject *coro, int64_t operation);'),
        ('py_coroutine_has_throw_arguments','i64',['ptr'],'int64_t py_coroutine_has_throw_arguments(PyObject *gen);'),
        ('py_coroutine_take_throw_arguments','ptr',['ptr'],'PyObject *py_coroutine_take_throw_arguments(PyObject *gen);'),
        ('py_await_throw_arguments','ptr',['ptr','ptr'],'PyObject *py_await_throw_arguments(PyObject *iterator, PyObject *args);'),
    ):
        returns,parameters,varargs=RUNTIME_SIGNATURES[name]
        assert str(returns) == result
        assert [str(parameter) for parameter in parameters] == args
        assert not varargs
        assert len(exported[name].args.args) == len(args)
        assert prototype in header
