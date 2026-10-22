"""Strict removal of the batch-index change; no production module imports."""

import ast
import copy


BASELINE_NATIVE_AST_SHA256 = "7a1fdd2fe237419759588b4e407a8bba8a6cfd973b8059aada53fd933ecfa7ef"
# ast.dump's text changed in CPython 3.15; ast.unparse gives the same text on
# 3.14 and 3.15, so either hash identifies the unchanged reference body.
BASELINE_NATIVE_UNPARSE_SHA256 = "2429ff2e8902abd956cda38e8513e2623699173640e1030d827e763a5f3a4d4b"


def without_shared_globals_index(function):
    result = copy.deepcopy(function)
    expected = ast.parse('''
def expected(_globals_index: list[dict[str, GlobalDef]] | None = None):
    if _globals_index is None:
        globals_by_name = {global_.name: global_ for global_ in globals_}
    elif _globals_index:
        globals_by_name = _globals_index[0]
    else:
        globals_by_name = {global_.name: global_ for global_ in globals_}
        _globals_index.append(globals_by_name)
''').body[0]
    assert len(result.args.defaults) == 1
    assert ast.dump(result.args.args[-1]) == ast.dump(expected.args.args[0])
    assert ast.dump(result.args.defaults[0]) == ast.dump(expected.args.defaults[0])
    assert ast.dump(result.body[1]) == ast.dump(expected.body[0])
    result.args.args.pop()
    result.args.defaults.clear()
    result.body[1] = copy.deepcopy(expected.body[0].body[0])
    return result
