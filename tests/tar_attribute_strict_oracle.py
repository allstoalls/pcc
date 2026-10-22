"""Source/IR-text oracles for the six tar-attribute compiler dependencies.

No compiler or runtime imports. Existing strict symbol checks remain unchanged;
only the two named compiler methods below admit their intended diagnostics.
Consumer IR and real Windows execution stay in test_windows_tar_attributes.
"""

import ast
import re


TAR_COMPILER_MODULES = (
    "pcc.backend.pe_x86_64",
    "pcc.frontends.python.codegen.platform_machine_abis",
    "pcc.frontends.python.codegen.unsafe_lowering",
    "pcc.frontends.python.codegen.windows_platform",
    "pcc.frontends.python.pipeline_freestanding",
    "pcc.frontends.python.type_infer",
)
CONSTANT_ONLY_MODULE = TAR_COMPILER_MODULES[1]
_UNSAFE = TAR_COMPILER_MODULES[2]
_WINDOWS = TAR_COMPILER_MODULES[3]
_FREESTANDING = TAR_COMPILER_MODULES[4]
_METHOD_PREFIX = "user_" + _UNSAFE.replace(".", "_") + "_UnsafeIntrinsicMixin_"
MUTATION_METHOD = _METHOD_PREFIX + "_emit_unsafe_file_mutation"
DISPATCH_METHOD = _METHOD_PREFIX + "_emit_unsafe_intrinsic_call"

# The approved source branch is deliberately frozen rather than reconstructed
# from the candidate. This check alone is not emitted-code evidence.
_UTIME_BRANCH = '''
if intrinsic == "utime_file":
    self._unsafe_expect_arity(intrinsic, expr, 2)
    if self._target_sys_platform_text() not in ("darwin", "linux"):
        raise NotImplementedError(
            "pcc.unsafe.utime_file supports owned Windows/Linux and Darwin libSystem"
        )
    path = self._unsafe_ptr_arg(expr.args[0])
    times = self._unsafe_ptr_arg(expr.args[1])
    function = self._declare_external_function("utime", _I32, [_CSTR, _CSTR])
    raw = self.builder.call(function, [path, times], name=self._fresh("unsafe.utime_file.i32"))
    return self.builder.sext(raw, _I64, name=self._fresh("unsafe.utime_file"))
'''
_WINDOWS_BRANCH = '''
if self._target_sys_platform_text() == "win32":
    from pcc.frontends.python.codegen.windows_platform import handles, emit
    if handles(intrinsic):
        return emit(self, intrinsic, expr)
'''
_MUTATION_DIAGNOSTICS = (
    "pcc.unsafe.open_file supports Darwin libSystem and Linux x86_64 raw syscalls",
    "pcc.unsafe.rename_file supports Darwin libSystem and Linux x86_64 raw syscalls",
    "pcc.unsafe.chmod_file supports Darwin libSystem and Linux x86_64 raw syscalls",
    "pcc.unsafe.utime_file supports owned Windows/Linux and Darwin libSystem",
    "pcc.unsafe.sync_file supports Darwin libSystem and Linux x86_64 raw syscalls",
    "pcc.unsafe internal intrinsic family mismatch",
)
_DEFINITION = re.compile(
    r'^define(?P<header>[^\n]*)@"?(?P<symbol>[A-Za-z_][\w.$]*)"?'
    r'\((?P<args>[^\n]*)\)[^\n]*\{\n(?P<body>.*?)^\}', re.M | re.S,
)
_GLOBAL = re.compile(r'^@(?P<symbol>"[^"]+"|[\w.$]+)\s*=\s*(?P<value>[^\n]+)', re.M)


def _reference(symbol):
    return r'@"?' + re.escape(symbol) + r'"?(?![\w.$])'


def _definition(section, symbol):
    matches = [match for match in _DEFINITION.finditer(section) if match.group("symbol") == symbol]
    assert len(matches) == 1, (symbol, "missing/duplicate definition")
    match = matches[0]
    assert "@" not in match.group("header"), (symbol, "ambiguous header")
    assert not re.search(r'^define\b', match.group("body"), re.M)
    assert "strict.nolib" not in match.group("body")
    assert not re.search(r'@"?py_cpy_', match.group("body"))
    return match


def _call(body, symbol):
    assert re.search(r'\bcall\b[^\n]*' + _reference(symbol) + r'\(', body), symbol


def _literal_symbols(section, value):
    encoded = ''.join('\\' + format(byte, '02X') for byte in value.encode()) + '\\00'
    symbols = {'@' + match.group('symbol').strip('"') for match in _GLOBAL.finditer(section)
               if 'c"' + encoded + '"' in match.group('value')}
    assert symbols, value
    return symbols


def _literal(section, body, value):
    symbols = _literal_symbols(section, value)
    # Static item arrays (constant tuple/aggregate tables the body builds
    # from) may hold the literal object. Only immutable constants qualify.
    constants = [('@' + match.group('symbol').strip('"'), match.group('value'))
                 for match in _GLOBAL.finditer(section)
                 if re.match(r'(?:\w+\s+)*constant\b', match.group('value'))]
    holders, pending = set(symbols), set(symbols)
    while pending:
        found = {symbol for symbol, value_text in constants if symbol not in holders
                 and any(re.search(_reference(held[1:]), value_text) for held in pending)}
        holders |= found
        pending = found
    assert any(re.search(_reference(symbol[1:]), body) for symbol in holders), value
    return symbols


_TOKEN = re.compile(r'[@%](?:"[^"]+"|[A-Za-z_.$][\w.$]*|\d+)')


def _tokens(text):
    return {token[0] + token[1:].strip('"') for token in _TOKEN.findall(text)}


def _arguments(text):
    # LLVM arguments can contain constant GEP expressions and aggregate types.
    depth, quoted, start, parts = 0, False, 0, []
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif not quoted:
            if char in '([{<':
                depth += 1
            elif char in ')]}>':
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
    parts.append(text[start:].strip())
    return parts if parts != [''] else []


def _zero_gep_source(instruction):
    match = re.fullmatch(r'getelementptr\s+(?:inbounds\s+)?(.*)', instruction)
    if match is None:
        return None
    text = match[1].strip()
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1]
    parts = _arguments(text)
    if (len(parts) < 3 or not re.fullmatch(r'ptr\s+[@%](?:"[^"]+"|[\w.$]+)', parts[1])
            or not all(re.fullmatch(r'i(?:32|64)\s+0', part) for part in parts[2:])):
        return None
    return _tokens(parts[1])


class _Flow:
    """Small fail-closed identity-flow oracle, not a general LLVM verifier.

    Only pointer copies and the exact owned root operations below propagate
    identities. Calls never inherit arbitrary operand identities. Whole IR is
    still checked by the pre-existing verifier; these edges establish binding.
    """
    def __init__(self, body, *, exact_addresses=False):
        self.calls, self.edges = [], {}
        casts = []
        for line in body.splitlines():
            assigned = re.match(r'\s*(%[^=]+?)\s*=\s*(.*)', line)
            result = next(iter(_tokens(assigned[1])), None) if assigned else None
            instruction = assigned[2] if assigned else line.strip()
            # The callee may follow an explicit function type: call ptr (ptr) @f(...).
            call = re.search(
                r'\bcall\s+(?:\w+\s+)*(?:\([^()]*\)\s*)?(@(?:"[^"]+"|[\w.$]+))\((.*)\)', instruction,
            )
            if call:
                callee = call[1][1:].strip('"')
                args = _arguments(call[2])
                self.calls.append((result, callee, args, instruction))
            if result and re.match(r'(?:getelementptr|bitcast)\b', instruction):
                sources = _tokens(instruction)
                if exact_addresses and instruction.startswith('getelementptr '):
                    sources = _zero_gep_source(instruction) or set()
                elif exact_addresses:
                    cast = re.fullmatch(r'bitcast\s+ptr\s+([@%](?:"[^"]+"|[\w.$]+))\s+to\s+ptr', instruction)
                    sources = _tokens(cast[1]) if cast else set()
                self._edge(result, sources)
                if instruction.startswith('bitcast ') or (exact_addresses and sources):
                    casts.append((result, sources))
            if result and instruction.startswith('load ptr, ptr '):
                self._edge(result, {'*' + token for token in _tokens(instruction)})
            store = re.match(r'store ptr (.*), ptr (.*)', instruction)
            if store:
                for destination in _tokens(store[2]):
                    self._edge('*' + destination, _tokens(store[1]))
        for result, callee, args, _line in self.calls:
            if callee in ('pcc_gc_store_root', 'pcc_gc_store_root_take') and len(args) == 2:
                for destination in _tokens(args[0]):
                    self._edge('*' + destination, _tokens(args[1]))
            elif callee in ('pcc_gc_root_copy_lease', 'pcc_gc_root_copy_borrowed_lease',
                            'pcc_gc_root_move') and len(args) == 2:
                for destination in _tokens(args[0]):
                    self._edge('*' + destination, {'*' + token for token in _tokens(args[1])})
            elif callee in ('pcc_gc_load_ptr', 'pcc_gc_load_borrowed_ptr') and len(args) == 2 and result:
                self._edge(result, {'*' + token for token in _tokens(args[1])})
            elif callee == 'pcc_gc_take_pinned_slot' and len(args) == 2 and result:
                self._edge(result, {'*' + token for token in _tokens(args[0])})
            elif callee == 'pcc_gc_retain' and len(args) == 1 and result:
                # An owned reference to the same object: incref, return the argument.
                self._edge(result, _tokens(args[0]))
        # Address aliases also name the same root contents.
        for destination, sources in tuple(self.edges.items()):
            if not destination.startswith('*'):
                for source in sources:
                    if not source.startswith('*'):
                        self._edge('*' + destination, {'*' + source})
        # A bitcast is the same address, so a root stored through it is the
        # source's content as well. Offsetting GEPs stay one-directional.
        for destination, sources in casts:
            for source in sources:
                self._edge('*' + source, {'*' + destination})

    def _edge(self, destination, sources):
        self.edges.setdefault(destination, set()).update(sources)

    def ancestors(self, argument, *, rooted=False):
        pending = list(_tokens(argument))
        if rooted:
            pending = ['*' + token for token in pending]
        seen = set()
        while pending:
            token = pending.pop()
            if token not in seen:
                seen.add(token)
                pending.extend(self.edges.get(token, ()))
        return seen

    def reaches(self, argument, identities, *, rooted=False):
        return bool(self.ancestors(argument, rooted=rooted) & identities)


def _dynamic_attribute_call(section, body, attribute, *, receiver=None):
    flow = _Flow(body)
    names = _literal_symbols(section, attribute)
    for result, callee, args, _line in flow.calls:
        if (result and callee in ('py_obj_getattr', 'py_obj_load_method') and len(args) >= 2
                and flow.reaches(args[1], names)
                and (receiver is None or flow.reaches(args[0], receiver))):
            for _output, sink, operands, _ in flow.calls:
                if (sink in ('py_obj_call', 'py_obj_call_method', 'py_obj_call_method_kwargs',
                             'py_obj_call_slots', 'py_obj_call_slots_sync') and operands
                        and flow.reaches(operands[0], {result}, rooted='_slots' in sink)):
                    return result
    raise AssertionError((attribute, 'no bound owned attribute call'))


def _global_root(body, module, table):
    symbol = '.modvar.' + module.replace('.', '_') + '.' + table
    assert re.search(_reference(symbol), body), symbol
    return {'*@' + symbol}


def _table_use(body, identities):
    flow = _Flow(body)
    consumers = ('py_set_contains', 'py_obj_contains', 'py_dict_contains', 'py_dict_getitem',
                 'py_obj_getitem', 'py_obj_getitem_i64', 'py_obj_subscript', 'py_obj_subscript_i64',
                 'py_tuple_getitem', 'py_obj_len',
                 'py_tuple_len', 'py_dict_keys', 'py_dict_get', 'py_dict_get_default', 'py_dict_get_default_slots', 'py_obj_iter',
                 'py_dict_update', 'py_dict_update_slots')
    assert any(callee in consumers and any(flow.reaches(arg, identities, rooted=callee.endswith('_slots'))
               for arg in args) for _result, callee, args, _line in flow.calls), 'no bound table operation'


def _imported_table(section, body, module, member):
    flow = _Flow(body)
    modules, members = _literal_symbols(section, module), _literal_symbols(section, member)
    for imported, callee, args, _line in flow.calls:
        if (imported and callee == 'py_compiled_module_import_by_name' and len(args) == 1
                and flow.reaches(args[0], modules)):
            for value, getter, operands, _ in flow.calls:
                if (value and getter == 'py_obj_getattr' and len(operands) == 2
                        and flow.reaches(operands[0], {imported}) and flow.reaches(operands[1], members)):
                    _table_use(body, {value})
                    return
    raise AssertionError((module, member, 'missing owned import/member consumer'))


def _owned_set_union_results(flow):
    """Prove a fresh set result without making union an identity edge.

    The owned py_obj_or dispatch calls py_set_union only when both actual
    operands are native sets. Constructor provenance proves that case here;
    a generic object OR, one missing operand or an unrelated call cannot do so.
    """
    constructors = {result for result, callee, args, instruction in flow.calls
                    if result and instruction.startswith('call ptr ')
                    and ((callee == 'py_set_new' and not args)
                         or (callee == 'py_set_from_iterable' and len(args) == 1
                             and args[0].startswith('ptr ')))}

    def constructor_origin(argument):
        pending, seen, origins = list(_tokens(argument)), set(), set()
        while pending:
            value = pending.pop()
            if value in seen:
                continue
            seen.add(value)
            if value in constructors:
                origins.add(value)
            elif flow.edges.get(value):
                pending.extend(flow.edges[value])
            else:
                # A constructor somewhere upstream is insufficient if the
                # same root also carries an unknown value or call result.
                return None
        return next(iter(origins)) if len(origins) == 1 else None

    values = set()
    for result, callee, args, instruction in flow.calls:
        if (not result or callee != 'py_obj_or' or len(args) != 2
                or not instruction.startswith('call ptr ')
                or not all(arg.startswith('ptr ') for arg in args)):
            continue
        left = constructor_origin(args[0])
        right = constructor_origin(args[1])
        if left is not None and right is not None and left != right:
            values.add(result)
    return values


def _initializer(section, module, tables, require_definition, bodies):
    initializer = '_pcc_py_module_top_' + module.replace('.', '_')
    require_definition(section, initializer, bodies=bodies)
    body = _definition(section, initializer).group('body')
    flow = _Flow(body)
    module_names = _literal_symbols(section, module)
    for table, constructors in tables:
        symbol = '.modvar.' + module.replace('.', '_') + '.' + table
        assert re.search(r'^' + _reference(symbol) + r'\s*=\s*[^\n]*\bglobal ptr\b', section, re.M)
        values = {result for result, callee, _args, _ in flow.calls if result and callee in constructors}
        if module == _UNSAFE and table == 'UNSAFE_INTRINSICS':
            # This registry combines its ordinary names with shared intrinsic
            # metadata. Require the fresh union itself to reach the global:
            # either operand alone, or a dormant unrelated union, is not enough.
            values = _owned_set_union_results(flow)
        assert values and flow.reaches('@"' + symbol + '"', values, rooted=True), (table, 'unbound construction')
        names = _literal_symbols(section, table)
        assert any(callee == 'py_module_attr_set' and len(args) == 3
                   and flow.reaches(args[0], module_names) and flow.reaches(args[1], names)
                   and flow.reaches(args[2], {'*@' + symbol})
                   for _result, callee, args, _ in flow.calls), (table, 'unbound named publication')
    return initializer, body


def _method_source(source):
    tree = ast.parse(source)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                 and node.name == "UnsafeIntrinsicMixin")
    methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}
    mutation, dispatch = methods["_emit_unsafe_file_mutation"], methods["_emit_unsafe_intrinsic_call"]
    for method in (mutation, dispatch):
        assert [arg.arg for arg in method.args.args] == ["self", "intrinsic", "expr"]
        assert ast.unparse(method.returns) == "ir.Value"
    expected = ast.dump(ast.parse(_UTIME_BRANCH).body[0], include_attributes=False)
    assert sum(ast.dump(node, include_attributes=False) == expected for node in mutation.body) == 1
    expected = ast.dump(ast.parse(_WINDOWS_BRANCH).body[0], include_attributes=False)
    assert sum(ast.dump(node, include_attributes=False) == expected for node in dispatch.body) == 1
    raises = [node for node in ast.walk(mutation) if isinstance(node, ast.Raise)]
    assert len(raises) == 6
    diagnostics = [node.exc.args[0].value for node in sorted(ast.walk(mutation), key=lambda node: getattr(node, "lineno", 0))
                   if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                   and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "NotImplementedError"
                   and len(node.exc.args) == 1 and isinstance(node.exc.args[0], ast.Constant)]
    assert tuple(diagnostics) == _MUTATION_DIAGNOSTICS
    expected = ast.dump(ast.parse('raise NotImplementedError(f"unknown pcc.unsafe intrinsic {intrinsic!r}")').body[0],
                        include_attributes=False)
    raises = [node for node in ast.walk(dispatch) if isinstance(node, ast.Raise)]
    assert len(raises) == 1 and ast.dump(raises[0], include_attributes=False) == expected
    return mutation, dispatch


def _exact_address(argument, assignments, symbols, gep_type, pending=()):
    """Resolve static addresses without granting offset GEPs object identity."""
    text = argument.strip()
    if text.startswith('ptr '):
        text = text[4:].strip()
    if re.fullmatch(r'[@%](?:"[^"]+"|[\w.$]+)', text):
        token = next(iter(_tokens(text)))
        if token.startswith('@'):
            return token if token in symbols else None
        if token in pending or token not in assignments:
            return None
        return _exact_address(assignments[token], assignments, symbols, gep_type, (*pending, token))
    cast = (re.fullmatch(r'bitcast\s+(ptr\s+[@%](?:"[^"]+"|[\w.$]+))\s+to\s+ptr', text)
            or re.fullmatch(r'bitcast\s*\(\s*(ptr\s+[@%](?:"[^"]+"|[\w.$]+))\s+to\s+ptr\s*\)', text))
    if cast:
        return _exact_address(cast[1], assignments, symbols, gep_type, pending)
    match = re.fullmatch(r'getelementptr\s+inbounds\s+(.*)', text)
    if match is None:
        return None
    payload = match[1].strip()
    if payload.startswith('(') and payload.endswith(')'):
        payload = payload[1:-1]
    parts = _arguments(payload)
    if (len(parts) != 4 or parts[0] != gep_type
            or not all(re.fullmatch(r'i(?:32|64)\s+0', part) for part in parts[2:])):
        return None
    return _exact_address(parts[1], assignments, symbols, gep_type, pending)


def _binding_origin(flow, argument, origins, *, rooted=False):
    """Require a single known origin, rejecting mixed non-null root contents."""
    pending = list(_tokens(argument))
    if len(pending) != 1:
        return None
    if rooted:
        pending = ['*' + pending[0]]
    seen, found = set(), set()
    while pending:
        token = pending.pop()
        if token in seen:
            continue
        seen.add(token)
        if token in origins:
            found.add(token)
        sources = flow.edges.get(token, ())
        if sources:
            pending.extend(sources)
        elif token not in origins:
            return None
    return next(iter(found)) if len(found) == 1 else None


def _signature_string_objects(globals_by_name, value):
    """Recognize pooled str objects, never equal-text C-string addresses."""
    data = value.encode()
    count = len(data) + 1
    encoded = ''.join('\\' + format(byte, '02X') for byte in data) + '\\00'
    array = r'\[' + str(count) + r'\s+x\s+i8\]'
    layout = r'\{\s*i64,\s*i32,\s*i32,\s*i64,\s*i64,\s*i64,\s*' + array + r'\s*\}'
    payload = (r'\{\s*i64\s+1,\s*i32\s+4,\s*i32\s+1,\s*i64\s+' + str(len(data))
               + r',\s*i64\s+-1,\s*i64\s+-1,\s*' + array + r'\s+c"' + re.escape(encoded) + r'"\s*\}')
    return {name for name, definitions in globals_by_name.items() if len(definitions) == 1
            and re.fullmatch(r'internal\s+(?:global|constant)\s+' + layout + r'\s+' + payload, definitions[0])}


def _binding_cstrings(section, value):
    data = value.encode()
    encoded = ''.join('\\' + format(byte, '02X') for byte in data) + '\\00'
    definitions = {}
    for match in _GLOBAL.finditer(section):
        name = '@' + match['symbol'].strip('"')
        definitions.setdefault(name, []).append(match['value'])
    pattern = (r'internal\s+constant\s+\[' + str(len(data) + 1)
               + r'\s+x\s+i8\]\s+c"' + re.escape(encoded) + r'"')
    return {name for name, values in definitions.items()
            if len(values) == 1 and re.fullmatch(pattern, values[0])}


def _static_method_signature_descriptors(section):
    """The exact no-default three-argument signature of these two methods."""
    globals_by_name = {}
    for match in _GLOBAL.finditer(section):
        name = '@' + match['symbol'].strip('"')
        globals_by_name.setdefault(name, []).append(match['value'])
    words = (4, 11, '__pcc_func_signature_v1__', 4, 7, 'self', 'intrinsic', 'expr',
             4, 7, 1, 1, 1, 4, 7, 12, 12, 12, 4, 7, 10, 10, 10)
    literals = {value: _signature_string_objects(globals_by_name, value)
                for value in words if isinstance(value, str)}
    descriptors = set()
    for name, definitions in globals_by_name.items():
        if len(definitions) != 1:
            continue
        match = re.fullmatch(r'internal\s+constant\s+\[23 x ptr\]\s+\[(.*)\]', definitions[0])
        if match is None:
            continue
        cells = _arguments(match[1])
        if len(cells) != len(words):
            continue
        valid = True
        for cell, expected in zip(cells, words):
            if isinstance(expected, int):
                valid = bool(re.fullmatch(r'ptr\s+inttoptr\s*\(i64\s+' + str(expected) + r'\s+to\s+ptr\)', cell))
            else:
                # Object leaves point at the object header, not its byte data.
                leaf = (re.fullmatch(r'ptr\s+(@(?:"[^"]+"|[\w.$]+))', cell)
                        or re.fullmatch(r'ptr\s+bitcast\s*\(ptr\s+(@(?:"[^"]+"|[\w.$]+))\s+to\s+ptr\)', cell))
                valid = bool(leaf and next(iter(_tokens(leaf[1]))) in literals[expected])
            if not valid:
                break
        if valid:
            descriptors.add(name)
    return descriptors


def _static_method_signatures(section, flow, assignments):
    descriptors = _static_method_signature_descriptors(section)
    return {result for result, callee, args, line in flow.calls
            if result and callee == 'py_static_aggregate_build' and line.startswith('call ptr ')
            and len(args) == 1 and args[0].startswith('ptr ')
            and _exact_address(args[0], assignments, descriptors, '[23 x ptr]') is not None}


def _require_method_class_binding(section, initializer_body, symbol):
    """Prove this real unsafe method's callable, signature and class owner."""
    assert symbol in (MUTATION_METHOD, DISPATCH_METHOD)
    flow = _Flow(initializer_body, exact_addresses=True)
    assignments = {}
    for line in initializer_body.splitlines():
        match = re.fullmatch(r'\s*(%[^=]+?)\s*=\s*(.*)', line)
        if match:
            token = next(iter(_tokens(match[1])))
            assert token not in assignments, (token, 'duplicate SSA definition')
            assignments[token] = match[2]
    method_name = symbol[len(_METHOD_PREFIX):]
    names = _binding_cstrings(section, method_name)
    class_names = _binding_cstrings(section, 'UnsafeIntrinsicMixin')
    classes = {result for result, callee, args, line in flow.calls
               if result and callee == 'py_class_new' and line.startswith('call ptr ') and len(args) == 5
               and all(arg.startswith(kind) for arg, kind in zip(args, ('ptr ', 'ptr ', 'i32 ', 'ptr ', 'i32 ')))
               and _exact_address(args[0], assignments, class_names, '[21 x i8]') is not None}
    assert len(classes) == 1, (symbol, 'missing/ambiguous class construction')
    class_global = '.class.' + _UNSAFE.replace('.', '_') + '.UnsafeIntrinsicMixin'
    declarations = [match for match in _GLOBAL.finditer(section) if match['symbol'].strip('"') == class_global]
    assert len(declarations) == 1 and re.fullmatch(r'(?:internal\s+)?global\s+ptr\s+null', declarations[0]['value'])
    assert _binding_origin(flow, '@"' + class_global + '"', classes, rooted=True), (symbol, 'unpublished class')
    captures = {result for result, constructor, operands, line in flow.calls
                if result and constructor == 'py_tuple_new' and operands == ['i64 0'] and line.startswith('call ptr ')}
    signatures = {result for result, constructor, operands, line in flow.calls
                  if result and constructor == 'py_tuple_new' and operands == ['i64 5'] and line.startswith('call ptr ')}
    magic = _literal_symbols(section, '__pcc_func_signature_v1__')
    signatures = {signature for signature in signatures
                  if any(setter == 'py_tuple_set_item' and len(operands) == 3
                         and _binding_origin(flow, operands[0], {signature})
                         and operands[1] == 'i64 0' and flow.reaches(operands[2], magic)
                         for _value, setter, operands, _ in flow.calls)}
    signatures |= _static_method_signatures(section, flow, assignments)
    descriptors = _static_method_signature_descriptors(section)
    modules = {value: _binding_cstrings(section, value) for value in (_UNSAFE, '__main__')}
    qualname = 'UnsafeIntrinsicMixin.' + method_name
    qualnames = _binding_cstrings(section, qualname)
    for _status, callee, args, line in flow.calls:
        if not line.startswith('call i64 '):
            continue
        if callee == 'py_func_new_signature_slots':
            if (len(args) != 5 or not all(arg.startswith('ptr ') for arg in args)
                    or not _binding_origin(flow, args[0], captures, rooted=True)
                    or not _binding_origin(flow, args[1], signatures, rooted=True)):
                continue
            entry, name, output = args[2], args[3], args[4]
        elif callee == 'py_func_new_static_def':
            # This runtime ABI builds empty captures and the same signature
            # under its own roots. Trace the caller's owned output in argument
            # 8, never the status or optional definition cache.
            if (len(args) != 9
                    or not all(args[index].startswith('ptr ') for index in (0, 1, 2, 3, 4, 5, 7, 8))
                    or args[5] != 'ptr null' or args[7] != 'ptr null'
                    or args[6] != 'i64 0'
                    or not re.fullmatch(r'ptr\s+[@%](?:"[^"]+"|[\w.$]+)', args[8])
                    or _exact_address(args[2], assignments, descriptors, '[23 x ptr]') is None
                    or not any(_exact_address(args[3], assignments, literals,
                                              '[' + str(len(value) + 1) + ' x i8]') is not None
                               for value, literals in modules.items())
                    or _exact_address(args[4], assignments, qualnames,
                                      '[' + str(len(qualname) + 1) + ' x i8]') is None):
                continue
            entry, name, output = args[0], args[1], args[8]
        else:
            continue
        if (_exact_address(entry, assignments, {'@' + symbol + '_method_native_adapter'}, '') is None
                or _exact_address(name, assignments, names, '[' + str(len(method_name) + 1) + ' x i8]') is None):
            continue
        callable_roots = {'*' + token for token in _tokens(output)}
        setters = {}
        for _result, setter, operands, _ in flow.calls:
            if (setter in ('py_class_add_method', 'py_class_setattr_raw') and len(operands) == 3
                    and _exact_address(operands[1], assignments, names, '[' + str(len(method_name) + 1) + ' x i8]') is not None
                    and _binding_origin(flow, operands[2], callable_roots)
                    and _binding_origin(flow, operands[0], classes)):
                setters[setter] = operands[0]
        if (set(setters) == {'py_class_add_method', 'py_class_setattr_raw'}
                and flow.ancestors(setters['py_class_add_method']) & flow.ancestors(setters['py_class_setattr_raw'])):
            return
    raise AssertionError((symbol, 'not bound to real class'))


def require_tar_unsafe_methods(source, section, initializer_body, require_definition):
    """Only these two real compiler methods may contain their own NI branches."""
    _method_source(source)
    mutation = _definition(section, MUTATION_METHOD)
    dispatch = _definition(section, DISPATCH_METHOD)
    adapters = []
    for symbol, match in ((MUTATION_METHOD, mutation), (DISPATCH_METHOD, dispatch)):
        assert re.fullmatch(r'\s+(?:external\s+)?ptr\s+', match.group("header")), symbol
        args = match.group("args").split(',')
        assert len(args) == 3 and all(re.match(r'\s*ptr\s+%', arg) for arg in args), symbol
        adapter = symbol + "_method_native_adapter"
        require_definition(section, adapter)
        adapter_definition = _definition(section, adapter)
        assert re.fullmatch(r'\s+(?:external\s+)?ptr\s+', adapter_definition.group('header'))
        adapter_args = adapter_definition.group('args').split(',')
        assert len(adapter_args) == 2 and all(re.match(r'\s*ptr\s+%', arg) for arg in adapter_args)
        _call(adapter_definition.group('body'), symbol)
        _require_method_class_binding(section, initializer_body, symbol)
        adapters.append(adapter)
        assert re.search(r'\bbr i1\b', match.group("body")), (symbol, "no supported branch")
    body = mutation.group("body")
    assert len(re.findall(r'\bcall\b[^\n]*@"?py_exc_new"?\(\s*i64\s+11\s*,', body)) == 6
    assert not re.search(r'@"?py_exc_new_with_value"?\(\s*i64\s+11\s*,', body)
    for callee in ("user_pcc_ir_ir_IRBuilder_call2", "user_pcc_ir_ir_IRBuilder_sext",
                   _METHOD_PREFIX + "_unsafe_expect_arity", _METHOD_PREFIX + "_unsafe_ptr_arg",
                   _METHOD_PREFIX + "_target_sys_platform_text"):
        _call(body, callee)
    for literal in ("utime_file", "utime", "unsafe.utime_file.i32", "unsafe.utime_file",
                    *_MUTATION_DIAGNOSTICS):
        _literal(section, body, literal)
    direct = "user_pcc_frontends_python_codegen_ir_decl_helpers_IrDeclHelperMixin__declare_external_function"
    if re.search(r'\bcall\b[^\n]*' + _reference(direct) + r'\(', body) is None:
        _dynamic_attribute_call(section, body, "_declare_external_function")
    body = dispatch.group("body")
    assert len(re.findall(r'\bcall\b[^\n]*@"?py_exc_new_with_value"?\(\s*i64\s+11\s*,', body)) == 1
    assert not re.search(r'@"?py_exc_new"?\(\s*i64\s+11\s*,', body)
    for callee in (MUTATION_METHOD, "user_" + _WINDOWS.replace(".", "_") + "_handles",
                   "user_" + _WINDOWS.replace(".", "_") + "_emit"):
        _call(body, callee)
    _literal(section, body, "win32")
    _literal(section, body, "unknown pcc.unsafe intrinsic ")
    return [MUTATION_METHOD, DISPATCH_METHOD, *adapters]


def require_tar_attribute_module(name, source, section, remaining_sections, require_definition, *, bodies=None):
    """Return extra real code symbols for the unchanged PIDX/object checks."""
    if name not in TAR_COMPILER_MODULES:
        return []
    dictionaries = ("py_dict_new", "py_dict_new_presized", "py_dict_from_static_pairs", "py_static_aggregate_build")
    sets = ("py_set_new", "py_set_from_iterable")
    tuples = ("py_tuple_new", "py_tuple_from_static_items", "py_static_aggregate_build")
    tables = {
        TAR_COMPILER_MODULES[0]: (("_KERNEL32", sets),),
        CONSTANT_ONLY_MODULE: (("PLATFORM_MACHINE_ABIS", dictionaries),),
        _UNSAFE: (("UNSAFE_INTRINSICS", sets), ("_UNSAFE_INTRINSIC_FAMILIES", tuples)),
        _WINDOWS: (("_SIGNATURES", dictionaries),),
        TAR_COMPILER_MODULES[5]: (("_UNSAFE_INTRINSIC_RETURN_TYPES", dictionaries),),
    }
    required = []
    if name in tables:
        initializer, initializer_body = _initializer(section, name, tables[name], require_definition, bodies)
        required.append(initializer)
        if name == _UNSAFE:
            required.extend(require_tar_unsafe_methods(source, section, initializer_body, require_definition))
    consumers = {
        TAR_COMPILER_MODULES[0]: (("system_dll", "_KERNEL32"),),
        _UNSAFE: (("_unsafe_intrinsic_family", "_UNSAFE_INTRINSIC_FAMILIES"),),
        _WINDOWS: (("handles", "_SIGNATURES"), ("emit", "_SIGNATURES")),
        TAR_COMPILER_MODULES[5]: (("_unsafe_intrinsic_return_type", "_UNSAFE_INTRINSIC_RETURN_TYPES"),),
    }
    for function, table in consumers.get(name, ()):
        symbol = "user_" + name.replace(".", "_") + "_" + function
        require_definition(section, symbol, bodies=bodies)
        body = _definition(section, symbol).group("body")
        _table_use(body, _global_root(body, name, table))
    if name == CONSTANT_ONLY_MODULE:
        # This module is deliberately before its real consumer in MODULES.
        consumer_section = remaining_sections[_FREESTANDING]
        symbol = "user_" + _FREESTANDING.replace(".", "_") + "_freestanding_allowed_external_symbols"
        require_definition(consumer_section, symbol)
        _imported_table(consumer_section, _definition(consumer_section, symbol).group("body"), name, "PLATFORM_MACHINE_ABIS")
    if name == _FREESTANDING:
        symbol = "user_" + name.replace(".", "_") + "_freestanding_allowed_external_symbols"
        body = _definition(section, symbol).group("body")
        _imported_table(section, body, CONSTANT_ONLY_MODULE, "PLATFORM_MACHINE_ABIS")
        _imported_table(section, body, _WINDOWS, "_SIGNATURES")
        _literal(section, body, "utime_file")
        _literal(section, body, "utime")
    if name == _WINDOWS:
        definition = _definition(section, "user_" + name.replace(".", "_") + "_emit")
        body = definition.group("body")
        args = _arguments(definition.group('args'))
        assert len(args) == 3 and all(arg.startswith('ptr ') for arg in args)
        owner = _tokens(args[0])
        _literal(section, body, "pcc_win_")
        _dynamic_attribute_call(section, body, "_declare_external_function", receiver=owner)
        flow = _Flow(body)
        builder_names = _literal_symbols(section, 'builder')
        builders = {result for result, callee, operands, _ in flow.calls
                    if result and callee == 'py_obj_getattr' and len(operands) == 2
                    and flow.reaches(operands[0], owner) and flow.reaches(operands[1], builder_names)}
        assert builders, 'missing owner.builder lookup'
        _dynamic_attribute_call(section, body, "call", receiver=builders)
    if name == _UNSAFE:
        tree = ast.parse(source)
        owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'UnsafeIntrinsicMixin')
        assert any(isinstance(node, ast.Assign) and len(node.targets) == 1
                   and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '_UNSAFE_INTRINSICS'
                   and isinstance(node.value, ast.Name) and node.value.id == 'UNSAFE_INTRINSICS' for node in owner.body)
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == '_is_unsafe_intrinsic')
        expected = ast.parse('return name in self._UNSAFE_INTRINSICS').body[0]
        assert len(method.body) == 1 and ast.dump(method.body[0], include_attributes=False) == ast.dump(expected, include_attributes=False)
        symbol = _METHOD_PREFIX + '_is_unsafe_intrinsic'
        require_definition(section, symbol)
        body = _definition(section, symbol).group('body')
        names = _literal_symbols(section, '_UNSAFE_INTRINSICS')
        flow = _Flow(body)
        getters = {result for result, callee, args, _ in flow.calls if result and callee == 'py_obj_getattr'
                   and len(args) == 2 and flow.reaches(args[1], names)}
        assert getters, 'missing class registry lookup'
        _table_use(body, getters)
        flow = _Flow(initializer_body)
        registry = _global_root(initializer_body, name, 'UNSAFE_INTRINSICS')
        assert any(callee == 'py_class_setattr_raw' and len(args) == 3 and flow.reaches(args[1], names)
                   and flow.reaches(args[2], registry) for _result, callee, args, _ in flow.calls)
        required.append(symbol)
    return required
